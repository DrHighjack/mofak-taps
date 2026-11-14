"""Export to Google Sheets. The app is the source of truth; the sheet is a mirror.

Configure with two env vars:
  GOOGLE_SERVICE_ACCOUNT_JSON  path to the service account key file
  GOOGLE_SHEET_ID              the id from the sheet's URL
Then share the sheet with the service account's email (Editor).
Tabs written: "Stream log", "Today", "Attribution". Each tab is overwritten, never appended.
"""
import os, csv, io, logging
from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Slot
from .db import to_local, fmt_time, now_utc, set_setting
from .logic import slot_hours, paste_line, slots_for_day
from .attribution import attribute

logger = logging.getLogger("streamclock.sheets")

LOG_HEADERS = ["Date", "Streamer", "Shop", "Start", "End", "Hours", "Paste line"]


class SheetsError(RuntimeError):
    """A Sheets dependency, authentication, API, or network failure."""


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")) and bool(os.environ.get("GOOGLE_SHEET_ID"))


def log_rows(db: Session, start: date | None = None, end: date | None = None) -> list[list]:
    q = select(Slot).order_by(Slot.start, Slot.id)
    if start:
        q = q.where(Slot.day >= start)
    if end:
        q = q.where(Slot.day <= end)
    rows = [LOG_HEADERS]
    for s in db.scalars(q):
        h = slot_hours(s)
        rows.append([to_local(s.start).strftime("%-m/%-d/%Y"), s.streamer.name, s.shop.name,
                     fmt_time(s.start), fmt_time(s.end), "" if h is None else f"{h:.2f}", paste_line(s)])
    return rows


def today_rows(db: Session, day: date) -> list[list]:
    rows = [["Streamer", "Shop", "Start", "End", "Hours", "Paste line", "Status"]]
    for s in slots_for_day(db, day):
        h = slot_hours(s)
        rows.append([s.streamer.name, s.shop.name, fmt_time(s.start), fmt_time(s.end),
                     "" if h is None else f"{h:.2f}", paste_line(s), s.status])
    return rows


def attribution_rows(db: Session, day: date) -> list[list]:
    res = attribute(db, day)
    rows = [["Slot", "Shop", "Orders", "Product sales", "Shipping", "Product + shipping"]]
    for l in res.lines:
        rows.append([l.label, l.shop, l.orders, f"{l.product:.2f}", f"{l.shipping:.2f}", f"{l.total:.2f}"])
    t = res.total
    rows.append(["Total", "", t.orders, f"{t.product:.2f}", f"{t.shipping:.2f}", f"{t.total:.2f}"])
    return rows


def to_csv(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def sync(db: Session, day: date | None = None) -> str:
    """Overwrite the three tabs; `day` selects Attribution, never the Today tab."""
    if not configured():
        logger.error("Google Sheets is not configured")
        raise SheetsError("Google Sheets isn't set up yet. Add GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID (see README).")
    try:
        import gspread
        from google.auth.exceptions import GoogleAuthError
        from requests.exceptions import RequestException
    except ImportError as exc:
        logger.exception("Google Sheets dependency is missing")
        raise SheetsError("A Google Sheets dependency is missing. Install requirements.txt.") from exc
    sync_time = to_local(now_utc())
    attribution_day = day or sync_time.date()
    today = sync_time.date()
    logs = log_rows(db)
    tab_rows = (
        ("Stream log", logs),
        ("Today", today_rows(db, today)),
        ("Attribution", attribution_rows(db, attribution_day)),
    )
    try:
        gc = gspread.service_account(filename=os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
        for title, rows in tab_rows:
            try:
                ws = sh.worksheet(title)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(title=title, rows=max(100, len(rows) + 10), cols=max(10, len(rows[0])))
            row_count = max(ws.row_count, len(rows))
            col_count = max(ws.col_count, max(map(len, rows)))
            if row_count != ws.row_count or col_count != ws.col_count:
                ws.resize(rows=row_count, cols=col_count)
            ws.clear()
            ws.update(range_name="A1", values=rows)
    except (gspread.exceptions.GSpreadException, GoogleAuthError, RequestException, OSError, ValueError) as exc:
        logger.exception("Google Sheets sync failed")
        raise SheetsError(f"Google Sheets sync failed: {exc}") from exc
    set_setting(db, "last_sheets_sync", sync_time.strftime("%-m/%-d/%Y %-I:%M %p"))
    db.commit()
    return f"Sent to Google Sheets: {len(logs) - 1} log rows, Today and Attribution for {attribution_day:%-m/%-d}."
