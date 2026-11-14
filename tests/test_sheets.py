from datetime import datetime
import sys
from types import SimpleNamespace
import pytest

from app import models as M, sheets as S
from app.db import to_utc
from conftest import get


class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.values = [["old", "stale", "row"]]
        self.clear_count = 0
        self.row_count = 100
        self.col_count = 10
        self.resize_calls = []

    def resize(self, rows, cols):
        self.row_count = rows
        self.col_count = cols
        self.resize_calls.append((rows, cols))

    def clear(self):
        self.values = []
        self.clear_count += 1

    def update(self, range_name, values):
        assert range_name == "A1"
        assert len(values) <= self.row_count
        self.values = [list(row) for row in values]


class FakeSheet:
    def __init__(self):
        self.tabs = {name: FakeWorksheet(name) for name in ("Stream log", "Today", "Attribution")}

    def worksheet(self, title):
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWorksheet(title)
        return self.tabs[title]


class FakeSheetsClient:
    def __init__(self, sheet):
        self.sheet = sheet

    def open_by_key(self, key):
        assert key == "sheet-id"
        return self.sheet


def install_sheets_mock(monkeypatch, sheet):
    class WorksheetNotFound(Exception):
        pass

    fake = SimpleNamespace(
        WorksheetNotFound=WorksheetNotFound,
        exceptions=SimpleNamespace(GSpreadException=WorksheetNotFound),
        service_account=lambda filename: FakeSheetsClient(sheet),
    )
    monkeypatch.setitem(sys.modules, "gspread", fake)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service-account.json")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-id")


def test_log_has_exact_a_to_g_headers_and_no_status(db):
    assert S.log_rows(db)[0] == ["Date", "Streamer", "Shop", "Start", "End", "Hours", "Paste line"]


def test_sync_overwrites_tabs_and_historical_send_keeps_today_current(db, monkeypatch):
    streamer = get(db, M.Streamer, "Streamer A")
    today_streamer = get(db, M.Streamer, "Streamer B")
    shop = get(db, M.Shop, "Shop A")
    historical_start = to_utc(datetime(2026, 11, 4, 10, 0))
    today_start = to_utc(datetime(2026, 11, 11, 10, 0))
    db.add_all([
        M.Slot(day=datetime(2026, 11, 4).date(), streamer_id=streamer.id, shop_id=shop.id,
               start=historical_start, end=to_utc(datetime(2026, 11, 4, 11, 0)), status="closed"),
        M.Slot(day=datetime(2026, 11, 11).date(), streamer_id=today_streamer.id, shop_id=shop.id,
               start=today_start, end=to_utc(datetime(2026, 11, 11, 11, 0)), status="closed"),
    ])
    db.commit()
    monkeypatch.setattr(S, "now_utc", lambda: to_utc(datetime(2026, 11, 11, 12, 0)))
    sheet = FakeSheet()
    install_sheets_mock(monkeypatch, sheet)

    for _ in range(2):
        message = S.sync(db, datetime(2026, 11, 4).date())
        assert "Attribution for 11/4" in message
        assert sheet.tabs["Stream log"].values[0] == ["Date", "Streamer", "Shop", "Start", "End", "Hours", "Paste line"]
        assert len(sheet.tabs["Stream log"].values) == 3
        assert sheet.tabs["Today"].values[1][0] == "Streamer B"
        assert len(sheet.tabs["Today"].values) == 2
        assert sheet.tabs["Attribution"].values[0][0] == "Slot"

    assert all(tab.clear_count == 2 for tab in sheet.tabs.values())
    assert all("stale" not in repr(tab.values) for tab in sheet.tabs.values())


def test_sync_raises_logged_explicit_error_and_does_not_claim_success(db, monkeypatch, caplog):
    class BrokenClient:
        def open_by_key(self, key):
            raise OSError("connection refused")

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service-account.json")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setitem(sys.modules, "gspread", SimpleNamespace(
        WorksheetNotFound=Exception,
        exceptions=SimpleNamespace(GSpreadException=type("APIError", (Exception,), {})),
        service_account=lambda filename: BrokenClient(),
    ))
    with pytest.raises(S.SheetsError, match="Google Sheets sync failed: connection refused"):
        S.sync(db)
    assert "Google Sheets sync failed" in caplog.text
    assert db.get(M.Setting, "last_sheets_sync").value == ""


def test_sync_expands_existing_worksheet_before_writing(db, monkeypatch):
    sheet = FakeSheet()
    install_sheets_mock(monkeypatch, sheet)
    rows = [S.LOG_HEADERS] + [["value"] * 7 for _ in range(150)]
    monkeypatch.setattr(S, "log_rows", lambda db: rows)
    S.sync(db)
    assert sheet.tabs["Stream log"].resize_calls == [(151, 10)]
    assert sheet.tabs["Stream log"].values == rows


def test_missing_dependency_raises_logged_sheets_error(db, monkeypatch, caplog):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "key.json")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setitem(sys.modules, "gspread", None)
    with pytest.raises(S.SheetsError, match="dependency is missing"):
        S.sync(db)
    assert "dependency is missing" in caplog.text


def test_programming_errors_are_not_hidden(db, monkeypatch):
    sheet = FakeSheet()
    install_sheets_mock(monkeypatch, sheet)
    monkeypatch.setattr(sheet, "worksheet", lambda title: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        S.sync(db)
