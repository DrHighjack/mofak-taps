from datetime import datetime
from app import logic as L, models as M
from app.db import to_utc
from conftest import get

PT = lambda s: to_utc(datetime.fromisoformat(s))
# 2026-11-04 is a Wednesday (weekday 2)


def add_rule(db, name, shop, wd, st, en):
    db.add(M.ScheduleRule(streamer_id=get(db, M.Streamer, name).id, shop_id=get(db, M.Shop, shop).id, weekday=wd, start_time=st, end_time=en))
    db.flush()


def test_empty_schedule_is_inert(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    assert L.scheduled_shop_now(db, streamer_a, PT("2026-11-04T10:00")) is None
    assert L.no_shows(db, datetime(2026, 11, 4).date(), PT("2026-11-04T23:00")) == []
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:00"))
    assert L.auto_close(db, PT("2026-11-04T10:59")) == [] and L.mismatch_hint(db, s) == ""


def test_smart_default_skips_shop_picker(db):
    add_rule(db, "Streamer A", "Shop A", 2, "10:00", "13:00")
    streamer_a = get(db, M.Streamer, "Streamer A")
    assert L.scheduled_shop_now(db, streamer_a, PT("2026-11-04T09:15")).name == "Shop A"   # 45 min early
    assert L.scheduled_shop_now(db, streamer_a, PT("2026-11-04T08:30")) is None            # too early
    assert L.scheduled_shop_now(db, streamer_a, PT("2026-11-04T13:00")) is None            # after end
    add_rule(db, "Streamer A", "Shop B", 2, "10:00", "13:00")                             # two shops at once: ask
    assert L.scheduled_shop_now(db, streamer_a, PT("2026-11-04T10:30")) is None


def test_auto_close_at_scheduled_end(db):
    add_rule(db, "Streamer A", "Shop A", 2, "10:00", "13:00")
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:03"))
    assert L.auto_close(db, PT("2026-11-04T13:29")) == []
    assert L.auto_close(db, PT("2026-11-04T13:31")) == [s]
    assert s.status == "auto_closed" and s.end == PT("2026-11-04T13:00") and L.slot_hours(s) == 2.95


def test_no_show_then_use_schedule(db):
    add_rule(db, "Streamer A", "Shop A", 2, "10:00", "13:00")
    day = datetime(2026, 11, 4).date()
    assert L.no_shows(db, day, PT("2026-11-04T10:20")) == []          # not yet
    ns = L.no_shows(db, day, PT("2026-11-04T10:31"))
    assert len(ns) == 1 and ns[0].label == "Streamer A on Shop A 10am-1pm"
    slot = L.use_schedule(db, day, ns[0].streamer.id, ns[0].shop.id, ns[0].start_time)
    assert slot.status == "estimated" and L.slot_label(slot) == "Streamer A 10am-1pm"
    assert L.no_shows(db, day, PT("2026-11-04T12:00")) == []


def test_dismiss_hides_no_show(db):
    add_rule(db, "Streamer A", "Shop A", 2, "10:00", "13:00")
    day = datetime(2026, 11, 4).date()
    ns = L.no_shows(db, day, PT("2026-11-04T11:00"))[0]
    L.dismiss(db, day, ns.streamer.id, ns.shop.id, ns.start_time)
    assert L.no_shows(db, day, PT("2026-11-04T11:00")) == []


def test_cancel_override_and_mismatch_hint(db):
    add_rule(db, "Streamer A", "Shop A", 2, "10:00", "13:00")
    db.add(M.ScheduleOverride(day=datetime(2026, 11, 4).date(), streamer_id=get(db, M.Streamer, "Streamer A").id,
                              shop_id=get(db, M.Shop, "Shop A").id, start_time="10:00", end_time="13:00", kind="cancel"))
    assert L.scheduled_for_day(db, datetime(2026, 11, 4).date()) == []
    add_rule(db, "Streamer B", "Shop A", 2, "13:00", "16:00")
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T13:05"))
    assert L.mismatch_hint(db, s) == "Schedule had Streamer B here"


def test_overnight_schedule_protects_clock_in_after_midnight(db):
    add_rule(db, "Streamer A", "Shop A", 2, "22:00", "03:00")
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-05T00:30"))
    assert L.auto_close(db, PT("2026-11-05T02:00")) == []
    assert L.auto_close(db, PT("2026-11-05T03:30")) == [s]
    assert s.end == PT("2026-11-05T03:00")


def test_tomorrow_schedule_can_pick_shop_for_early_tap(db):
    add_rule(db, "Streamer A", "Shop A", 3, "00:15", "03:00")
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    assert L.scheduled_shop_now(db, streamer_a, PT("2026-11-04T23:30")).id == shop_a.id
    s = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T23:30"))
    assert L.auto_close(db, PT("2026-11-05T00:30")) == []
    assert L.scheduled_end_for(db, s) == PT("2026-11-05T03:00")


def test_hidden_names_and_shops_do_not_create_schedule_prompts(db):
    add_rule(db, "Streamer A", "Shop A", 2, "10:00", "13:00")
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    streamer_a.active = False
    assert L.scheduled_for_day(db, datetime(2026, 11, 4).date()) == []
    streamer_a.active, shop_a.active = True, False
    assert L.scheduled_for_day(db, datetime(2026, 11, 4).date()) == []
