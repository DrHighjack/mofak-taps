"""Clock-in / clock-out rules and the schedule safety net.

Everything here is pure logic on a Session; the web layer only renders what it returns.
"""
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import (Streamer, Shop, Slot, ScheduleRule, ScheduleOverride, Dismissal)
from .db import (now_utc, to_local, to_utc, local_day, fmt_time, fmt_paste_time,
                 fmt_time_short, local_dt, setting_int, get_setting, audit)


class ClockError(Exception):
    """A refusal. The message is shown to the streamer for 5 seconds."""


def lock_streamer(db: Session, streamer_id: int):
    return db.scalars(select(Streamer).where(Streamer.id == streamer_id).with_for_update()
                      .execution_options(populate_existing=True)).one()


def lock_shop(db: Session, shop_id: int):
    return db.scalars(select(Shop).where(Shop.id == shop_id).with_for_update()
                      .execution_options(populate_existing=True)).one()


def late_slot(db: Session, streamer: Streamer, shop: Shop) -> Slot | None:
    latest = db.scalars(select(Slot).where(Slot.streamer_id == streamer.id, Slot.shop_id == shop.id)
                        .order_by(Slot.start.desc(), Slot.id.desc())
                        .execution_options(populate_existing=True)).first()
    if latest and latest.status == "auto_closed" and latest.note not in ("reviewed", "late clock-out, needs review"):
        return latest
    return None


def check_clock(db: Session, streamer: Streamer, shop: Shop, action: str):
    if streamer is None or shop is None or not streamer.active or not shop.active:
        raise ClockError("That name or shop is hidden. Ask Admin to check it.")
    if action not in ("in", "out", "late_out"):
        raise ClockError("Choose Clock in or Clock out.")
    existing = open_slot(db, streamer, shop)
    if action == "in":
        if existing:
            raise ClockError(f"{streamer.name} is already clocked in on {shop.name} (since {fmt_time(existing.start)})")
        if get_setting(db, "allow_both_shops") != "1":
            other = db.scalars(select(Slot).where(Slot.streamer_id == streamer.id, Slot.end.is_(None))).first()
            if other:
                raise ClockError(f"{streamer.name} is still clocked in on {other.shop.name}. Clock out there first.")
        return None
    if existing:
        return existing
    late = late_slot(db, streamer, shop)
    if late:
        return late
    raise ClockError(f"{streamer.name} is not clocked in on {shop.name}")


# ---- core rules --------------------------------------------------------------
def open_slot(db: Session, streamer: Streamer, shop: Shop) -> Slot | None:
    return db.scalars(
        select(Slot).where(Slot.streamer_id == streamer.id, Slot.shop_id == shop.id,
                           Slot.end.is_(None)).order_by(Slot.start.desc())
        .execution_options(populate_existing=True)
    ).first()


def open_slots(db: Session) -> list[Slot]:
    return list(db.scalars(select(Slot).where(Slot.end.is_(None)).order_by(Slot.start)))


def clock_in(db: Session, streamer: Streamer, shop: Shop, at: datetime | None = None, who="kiosk") -> Slot:
    at = at or now_utc()
    lock_streamer(db, streamer.id)
    lock_shop(db, shop.id)
    check_clock(db, streamer, shop, "in")
    slot = Slot(day=local_day(at), streamer_id=streamer.id, shop_id=shop.id, start=at, status="open")
    db.add(slot)
    db.flush()
    audit(db, who, "clock_in", "slot", slot.id, after=slot_dict(slot))
    return slot


def clock_out(db: Session, streamer: Streamer, shop: Shop, at: datetime | None = None, who="kiosk") -> Slot:
    at = at or now_utc()
    lock_streamer(db, streamer.id)
    slot = check_clock(db, streamer, shop, "out")
    if at < slot.start:
        raise ClockError("The end cannot be before the start. Ask Admin to check the time.")
    before = slot_dict(slot)
    was_auto_closed = slot.status == "auto_closed"
    slot.end = at
    slot.status = "auto_closed" if was_auto_closed else "closed"
    if was_auto_closed:
        slot.note = "late clock-out, needs review"
    audit(db, who, "late_clock_out" if was_auto_closed else "clock_out", "slot", slot.id,
          before=before, after=slot_dict(slot))
    return slot


def slot_hours(slot: Slot) -> float | None:
    if slot.end is None:
        return None
    return round((slot.end - slot.start).total_seconds() / 3600, 2)


def paste_line(slot: Slot) -> str:
    if slot.end is None:
        return ""
    return f"{slot.streamer.name} - {slot.shop.name} - {fmt_paste_time(slot.start)} - {fmt_paste_time(slot.end)}"


def slot_label(slot: Slot) -> str:
    end = fmt_time_short(slot.end) if slot.end else "open"
    return f"{slot.streamer.name} {fmt_time_short(slot.start)}-{end}"


def slot_dict(slot: Slot) -> dict:
    return {"id": slot.id, "day": str(slot.day), "streamer_id": slot.streamer_id, "shop_id": slot.shop_id,
            "start": str(slot.start), "end": str(slot.end) if slot.end else None, "status": slot.status,
            "note": slot.note}


def slots_for_day(db: Session, day: date) -> list[Slot]:
    return list(db.scalars(select(Slot).where(Slot.day == day).order_by(Slot.start, Slot.id)))


# ---- schedule (the safety net) ------------------------------------------------
@dataclass
class Scheduled:
    streamer: Streamer
    shop: Shop
    start: datetime   # naive UTC
    end: datetime     # naive UTC
    start_time: str   # HH:MM Pacific, for dismissal matching

    @property
    def label(self) -> str:
        return f"{self.streamer.name} on {self.shop.name} {fmt_time_short(self.start)}-{fmt_time_short(self.end)}"


def scheduled_for_day(db: Session, day: date) -> list[Scheduled]:
    """Weekly rules for that weekday, plus 'add' overrides, minus 'cancel' overrides."""
    out: list[Scheduled] = []
    rules = db.scalars(select(ScheduleRule).where(ScheduleRule.active.is_(True), ScheduleRule.weekday == day.weekday()))
    for r in rules:
        out.append(_mk(r.streamer, r.shop, day, r.start_time, r.end_time))
    for o in db.scalars(select(ScheduleOverride).where(ScheduleOverride.day == day)):
        if o.kind == "add":
            out.append(_mk(o.streamer, o.shop, day, o.start_time, o.end_time))
        else:
            out = [s for s in out if not (s.streamer.id == o.streamer_id and s.shop.id == o.shop_id and s.start_time == o.start_time)]
    return sorted((s for s in out if s.streamer.active and s.shop.active), key=lambda s: s.start)


def _mk(streamer, shop, day, st, en) -> Scheduled:
    s = local_dt(day, st)
    e = local_dt(day, en)
    if e <= s:
        e += timedelta(days=1)   # crosses midnight
    return Scheduled(streamer, shop, to_utc(s), to_utc(e), st)


def scheduled_shop_now(db: Session, streamer: Streamer, at: datetime | None = None) -> Shop | None:
    """Smart default: exactly one scheduled slot for this streamer covering now (or starting soon)."""
    at = at or now_utc()
    before = timedelta(minutes=setting_int(db, "smart_default_before_min"))
    hits = set()
    for day in (local_day(at) - timedelta(days=1), local_day(at), local_day(at) + timedelta(days=1)):
        for s in scheduled_for_day(db, day):
            if s.streamer.id == streamer.id and s.shop.active and (s.start - before) <= at < s.end:
                hits.add(s.shop.id)
    if len(hits) == 1:
        return db.get(Shop, hits.pop())
    return None


def scheduled_end_for(db: Session, slot: Slot) -> datetime | None:
    """Scheduled end matching an open slot (same streamer+shop, start within the window)."""
    before = timedelta(minutes=setting_int(db, "smart_default_before_min"))
    ends = []
    for day in (slot.day - timedelta(days=1), slot.day, slot.day + timedelta(days=1)):
        for s in scheduled_for_day(db, day):
            if s.streamer.id == slot.streamer_id and s.shop.id == slot.shop_id and (s.start - before) <= slot.start < s.end:
                ends.append(s.end)
    return max(ends) if ends else None


def auto_close(db: Session, at: datetime | None = None) -> list[Slot]:
    """Close slots left open. Scheduled end + grace if the schedule knows the slot, else N hours."""
    at = at or now_utc()
    grace = timedelta(minutes=setting_int(db, "auto_close_grace_min"))
    fallback = timedelta(hours=setting_int(db, "auto_close_hours"))
    closed = []
    for candidate in open_slots(db):
        lock_streamer(db, candidate.streamer_id)
        slot = db.get(Slot, candidate.id, populate_existing=True)
        if slot.end is not None:
            continue
        sched_end = scheduled_end_for(db, slot)
        if sched_end is not None:
            if at >= sched_end + grace:
                end_at, note = sched_end, "closed at scheduled end"
            else:
                continue
        elif at >= slot.start + fallback:
            end_at, note = slot.start + fallback, f"closed after {int(fallback.total_seconds()//3600)} hours"
        else:
            continue
        before = slot_dict(slot)
        slot.end, slot.status, slot.note = end_at, "auto_closed", note
        audit(db, "system", "auto_close", "slot", slot.id, before=before, after=slot_dict(slot))
        closed.append(slot)
    return closed


def no_shows(db: Session, day: date, at: datetime | None = None) -> list[Scheduled]:
    """Scheduled slots with no clock-in by start + N min, not dismissed."""
    at = at or now_utc()
    wait = timedelta(minutes=setting_int(db, "noshow_after_min"))
    slots = slots_for_day(db, day)
    dismissed = {(d.streamer_id, d.shop_id, d.start_time) for d in db.scalars(select(Dismissal).where(Dismissal.day == day))}
    out = []
    for s in scheduled_for_day(db, day):
        if at < s.start + wait:
            continue
        if (s.streamer.id, s.shop.id, s.start_time) in dismissed:
            continue
        covered = any(sl.streamer_id == s.streamer.id and sl.shop_id == s.shop.id
                      and sl.start < s.end and (sl.end is None or sl.end > s.start) for sl in slots)
        if not covered:
            out.append(s)
    return out


def use_schedule(db: Session, day: date, streamer_id: int, shop_id: int, start_time: str, who="Admin") -> Slot | None:
    """Admin clicked 'Use schedule': create an estimated slot from the scheduled one."""
    lock_streamer(db, streamer_id)
    for s in scheduled_for_day(db, day):
        if s.streamer.id == streamer_id and s.shop.id == shop_id and s.start_time == start_time:
            existing = db.scalars(select(Slot).where(
                Slot.streamer_id == streamer_id, Slot.shop_id == shop_id,
                Slot.start < s.end, (Slot.end.is_(None) | (Slot.end > s.start)))).first()
            if existing:
                raise ClockError("A slot already covers this schedule. Fix that slot instead.")
            slot = Slot(day=day, streamer_id=streamer_id, shop_id=shop_id, start=s.start, end=s.end,
                        status="estimated", note="from schedule")
            db.add(slot)
            db.flush()
            audit(db, who, "use_schedule", "slot", slot.id, after=slot_dict(slot))
            return slot
    raise ClockError("That scheduled slot changed. Refresh the page and try again.")


def dismiss(db: Session, day: date, streamer_id: int, shop_id: int, start_time: str, who="Admin"):
    db.add(Dismissal(day=day, streamer_id=streamer_id, shop_id=shop_id, start_time=start_time))
    audit(db, who, "dismiss_noshow", "schedule", None, after={"day": str(day), "streamer_id": streamer_id, "shop_id": shop_id, "start": start_time})


def mismatch_hint(db: Session, slot: Slot) -> str:
    """'Schedule had Streamer B here' when a logged slot overlaps someone else's scheduled slot on the same shop."""
    for s in scheduled_for_day(db, slot.day):
        if s.shop.id == slot.shop_id and s.streamer.id != slot.streamer_id:
            end = slot.end or (slot.start + timedelta(hours=1))
            if slot.start < s.end and end > s.start:
                # only hint if the logged streamer is NOT also scheduled then
                own = any(o.streamer.id == slot.streamer_id and o.shop.id == slot.shop_id and slot.start < o.end and end > o.start
                          for o in scheduled_for_day(db, slot.day))
                if not own:
                    return f"Schedule had {s.streamer.name} here"
    return ""
