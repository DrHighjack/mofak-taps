import json
import pytest
from sqlalchemy import select

import import_log
from app import models as M
from app.db import to_local


def row(day="11/04/2026", streamer=" Streamer A ", shop=" Shop A ", start="11:30 PM", end="12:30 AM"):
    return {"Date": day, "Streamer": streamer, "Shop": shop, "Start": start, "End": end}


def test_import_rolls_midnight_end_to_next_local_day_and_audits_full_values(db):
    added, skipped = import_log.import_rows([row()], "old-stream-log.csv", db)
    slot = db.scalars(select(M.Slot)).one()
    assert (added, skipped) == (1, 0)
    assert slot.day.isoformat() == "2026-11-04"
    assert to_local(slot.start).isoformat() == "2026-11-04T23:30:00-08:00"
    assert to_local(slot.end).isoformat() == "2026-11-05T00:30:00-08:00"
    audit = db.scalars(select(M.AuditLog)).one()
    assert audit.before == ""
    after = json.loads(audit.after)
    assert after["source"] == "old-stream-log.csv"
    assert after["row"] == 2
    assert after["streamer"] == "Streamer A"
    assert after["shop"] == "Shop A"
    assert {key: value for key, value in after.items() if key not in {"source", "row", "streamer", "shop"}} == {
        "id": slot.id,
        "day": "2026-11-04",
        "streamer_id": slot.streamer_id,
        "shop_id": slot.shop_id,
        "start": "2026-11-05 07:30:00",
        "end": "2026-11-05 08:30:00",
        "status": "closed",
        "note": "imported",
    }


def test_import_repeat_is_idempotent(db):
    assert import_log.import_rows([row()], "same.csv", db) == (1, 0)
    assert import_log.import_rows([row()], "same.csv", db) == (0, 1)
    assert len(list(db.scalars(select(M.Slot)))) == 1
    assert len(list(db.scalars(select(M.AuditLog)))) == 1


def test_fall_back_ambiguous_end_uses_later_real_instant(db):
    values = row(day="11/01/2026", start="1:30 AM", end="1:15 AM")
    assert import_log.import_rows([values], "dst.csv", db) == (1, 0)
    slot = db.scalars(select(M.Slot)).one()
    assert (slot.end - slot.start).total_seconds() == 45 * 60


def test_nonexistent_dst_local_time_is_rejected(db):
    with pytest.raises(import_log.ImportLogError, match="daylight-saving change"):
        import_log.import_rows(
            [row(day="03/08/2026", start="2:30 AM", end="3:30 AM")],
            "dst-gap.csv",
            db,
        )
    assert db.scalars(select(M.Slot)).first() is None


def test_midnight_rollover_uses_real_next_date_before_dst_validation(db):
    import_log.import_rows([row(day="03/08/2026", start="11:30 PM", end="2:30 AM")], "dst.csv", db)
    slot = db.scalars(select(M.Slot)).one()
    assert to_local(slot.end).isoformat() == "2026-03-09T02:30:00-07:00"
    assert (slot.end - slot.start).total_seconds() == 3 * 3600


def test_entire_file_is_validated_before_any_rows_are_written(db):
    valid = row()
    invalid = row(day="not a date", streamer="New person", shop="New shop")
    with pytest.raises(import_log.ImportLogError, match="row 3"):
        import_log.import_rows([valid, invalid], "bad.csv", db)
    assert db.scalars(select(M.Slot)).first() is None
    assert db.scalars(select(M.Streamer).where(M.Streamer.name == "New person")).first() is None
    assert db.scalars(select(M.Shop).where(M.Shop.name == "New shop")).first() is None


def test_required_columns_and_values_are_checked_and_trimmed(db):
    with pytest.raises(import_log.ImportLogError, match="missing required"):
        import_log.import_rows([{"Date": "11/04/2026", "Streamer": "Streamer A", "Shop": "Shop A", "Start": ""}],
                               "missing.csv", db)
    assert import_log.import_rows([row(end="")], "open.csv", db) == (1, 0)
    slot = db.scalars(select(M.Slot)).one()
    assert slot.streamer.name == "Streamer A"
    assert slot.shop.name == "Shop A"
    assert slot.end is None and slot.status == "open"


def test_module_import_has_callable_without_running_cli_or_initializing_database():
    assert callable(import_log.main)
    assert callable(import_log.import_csv)


def test_csv_header_whitespace_trimmed_and_wrong_shape_rejected(db, monkeypatch):
    import io
    good = " Date , Streamer , Shop , Start , End \n11/04/2026,Streamer A,Shop A,10:00,11:00\n"
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO(good))
    assert import_log.import_csv("padded.csv", db) == (1, 0)
    for data in (
        "Date,Streamer,Shop,Start,End\n11/04/2026,Streamer A,Shop A,10:00\n",
        "Date,Streamer,Shop,Start,End\n11/04/2026,Streamer A,Shop A,10:00,11:00,extra\n",
    ):
        monkeypatch.setattr("builtins.open", lambda *args, _data=data, **kwargs: io.StringIO(_data))
        with pytest.raises(import_log.ImportLogError, match="wrong number"):
            import_log.import_csv("bad.csv", db)


def test_import_locks_streamer_before_duplicate_lookup(db, monkeypatch):
    locks = []
    real_lock = import_log.lock_streamer
    def lock(session, streamer_id):
        locks.append(streamer_id)
        return real_lock(session, streamer_id)
    monkeypatch.setattr(import_log, "lock_streamer", lock)
    import_log.import_rows([row(), row()], "duplicate.csv", db)
    assert len(locks) == 2 and locks[0] == locks[1]


def test_cli_integrity_failure_has_plain_message(monkeypatch, capsys):
    from sqlalchemy.exc import IntegrityError
    monkeypatch.setattr(import_log, "init_db", lambda: None)
    def conflict(*args):
        raise IntegrityError("secret SQL", {}, Exception("secret database detail"))
    monkeypatch.setattr(import_log, "import_csv", conflict)
    assert import_log.main(["conflict.csv"]) == 1
    error = capsys.readouterr().err
    assert "No rows were committed" in error
    assert "secret" not in error
