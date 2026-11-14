from datetime import datetime, timedelta
import pytest
from app import logic as L, models as M
from app.db import to_utc, set_setting
from conftest import get

PT = lambda s: to_utc(datetime.fromisoformat(s))   # "2026-11-04T10:02" Pacific -> naive UTC


def test_clock_in_creates_row_with_start(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:02"))
    assert s.end is None and s.status == "open" and str(s.day) == "2026-11-04"


def test_double_clock_in_refused(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:02"))
    with pytest.raises(L.ClockError, match="already clocked in on Shop A \\(since 10:02 AM\\)"):
        L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:30"))


def test_clock_out_without_open_slot_refused(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    with pytest.raises(L.ClockError, match="Streamer A is not clocked in on Shop A"):
        L.clock_out(db, streamer_a, shop_a)
    assert db.query(M.Slot).count() == 0


def test_clock_out_matches_by_name_and_shop_not_last_row(db):
    streamer_a, streamer_b, shop_a, one = get(db, M.Streamer, "Streamer A"), get(db, M.Streamer, "Streamer B"), get(db, M.Shop, "Shop A"), get(db, M.Shop, "Shop B")
    e = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:00"))
    L.clock_in(db, streamer_b, shop_a, PT("2026-11-04T10:30"))
    L.clock_in(db, streamer_a, one, PT("2026-11-04T11:00"))     # same person, both shops: allowed
    out = L.clock_out(db, streamer_a, shop_a, PT("2026-11-04T13:00"))
    assert out.id == e.id and L.slot_hours(out) == 3.0
    assert L.open_slot(db, streamer_b, shop_a) is not None and L.open_slot(db, streamer_a, one) is not None


def test_both_shops_can_be_blocked(db):
    set_setting(db, "allow_both_shops", "0")
    streamer_a, shop_a, one = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A"), get(db, M.Shop, "Shop B")
    L.clock_in(db, streamer_a, shop_a)
    with pytest.raises(L.ClockError, match="still clocked in on Shop A"):
        L.clock_in(db, streamer_a, one)


def test_paste_line_and_label(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:02"))
    L.clock_out(db, streamer_a, shop_a, PT("2026-11-04T13:00"))
    assert L.paste_line(s) == "Streamer A - Shop A - 10:02AM - 1:00PM"
    assert L.slot_label(s) == "Streamer A 10:02am-1pm"


def test_midnight_hours_positive(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T22:00"))
    L.clock_out(db, streamer_a, shop_a, PT("2026-11-05T01:30"))
    assert L.slot_hours(s) == 3.5 and str(s.day) == "2026-11-04"


def test_auto_close_fallback_hours(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:00"))
    assert L.auto_close(db, PT("2026-11-04T10:59")) == []
    closed = L.auto_close(db, PT("2026-11-04T11:00"))
    assert closed == [s] and s.status == "auto_closed" and L.slot_hours(s) == 1.0


def test_audit_written(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    L.clock_in(db, streamer_a, shop_a)
    L.clock_out(db, streamer_a, shop_a)
    acts = [a.action for a in db.query(M.AuditLog)]
    assert acts == ["clock_in", "clock_out"]
