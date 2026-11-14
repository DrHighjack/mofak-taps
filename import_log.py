"""Import a CSV export of the old Stream log tab."""
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv()

from app import models as M
from app.db import SessionLocal, TZ, UTC, audit, init_db, local_day
from app.logic import lock_streamer

FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


class ImportLogError(ValueError):
    """The input CSV has a row that cannot be imported safely."""


def parse(day: str, clock: str) -> datetime:
    value = f"{day.strip()} {clock.strip()}"
    for fmt in FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"can't read date/time {value!r}")


def _local_candidates(value: datetime) -> list[datetime]:
    """Return real UTC instants for a local wall time, rejecting DST gaps."""
    candidates = []
    for fold in (0, 1):
        local = value.replace(tzinfo=TZ, fold=fold)
        utc = local.astimezone(UTC)
        if utc.astimezone(TZ).replace(tzinfo=None) == value:
            naive_utc = utc.replace(tzinfo=None)
            if naive_utc not in candidates:
                candidates.append(naive_utc)
    if not candidates:
        raise ValueError(f"{value:%Y-%m-%d %H:%M} is not a valid local time (daylight-saving change)")
    return sorted(candidates)


def _start_utc(value: datetime) -> datetime:
    return _local_candidates(value)[0]


def _end_utc(value: datetime, start_local: datetime, start_utc: datetime) -> datetime:
    # During the autumn clock change, a lower wall-clock time may still be later.
    # If not, interpret an earlier end clock as the following local calendar day.
    if value.time() < start_local.time():
        try:
            candidates = [candidate for candidate in _local_candidates(value) if candidate >= start_utc]
        except ValueError:
            candidates = []
        if candidates:
            return candidates[0]
        value += timedelta(days=1)
    candidates = [candidate for candidate in _local_candidates(value) if candidate >= start_utc]
    if candidates:
        return candidates[0]
    raise ValueError("end time is before start time")


def _validated_rows(rows: Iterable[dict[str, str | None]], source: str):
    parsed = []
    for line, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ImportLogError(f"{source}, row {line}: wrong number of CSV fields")
        values = {key: (value or "").strip() for key, value in row.items() if key}
        missing = [key for key in ("Date", "Streamer", "Shop", "Start") if not values.get(key)]
        if missing:
            raise ImportLogError(f"{source}, row {line}: missing required value(s): {', '.join(missing)}")
        try:
            start_local = parse(values["Date"], values["Start"])
            start = _start_utc(start_local)
            end_local = parse(values["Date"], values["End"]) if values.get("End") else None
            end = _end_utc(end_local, start_local, start) if end_local else None
        except ValueError as exc:
            raise ImportLogError(f"{source}, row {line}: {exc}") from exc
        parsed.append({
            "line": line,
            "streamer": values["Streamer"],
            "shop": values["Shop"],
            "start": start,
            "end": end,
            "day": local_day(start),
        })
    return parsed


def import_rows(rows: Iterable[dict[str, str | None]], source: str, db: Session) -> tuple[int, int]:
    """Validate the entire input before writing; repeated imports skip existing slots."""
    parsed = _validated_rows(rows, source)
    added = skipped = 0
    for item in parsed:
        streamer_name, shop_name = item["streamer"], item["shop"]
        streamer = db.scalars(select(M.Streamer).where(M.Streamer.name == streamer_name)).first()
        shop = db.scalars(select(M.Shop).where(M.Shop.name == shop_name)).first()
        if streamer is None:
            streamer = M.Streamer(name=streamer_name, sort=99)
            db.add(streamer)
            db.flush()
        if shop is None:
            shop = M.Shop(name=shop_name, sort=99)
            db.add(shop)
            db.flush()

        lock_streamer(db, streamer.id)
        duplicate = db.scalars(select(M.Slot).where(
            M.Slot.streamer_id == streamer.id,
            M.Slot.shop_id == shop.id,
            M.Slot.start == item["start"],
        )).first()
        if duplicate:
            skipped += 1
            continue

        slot = M.Slot(
            day=item["day"],
            streamer_id=streamer.id,
            shop_id=shop.id,
            start=item["start"],
            end=item["end"],
            status="closed" if item["end"] else "open",
            note="imported",
        )
        db.add(slot)
        db.flush()
        audit(
            db,
            "import",
            "import",
            "slot",
            slot.id,
            before=None,
            after={
                "source": source,
                "row": item["line"],
                "streamer": streamer.name,
                "shop": shop.name,
                "id": slot.id,
                "day": str(slot.day),
                "streamer_id": slot.streamer_id,
                "shop_id": slot.shop_id,
                "start": slot.start,
                "end": slot.end,
                "status": slot.status,
                "note": slot.note,
            },
        )
        added += 1
    db.commit()
    return added, skipped


def import_csv(path: str | Path, db: Session) -> tuple[int, int]:
    source = str(path)
    with open(path, newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames:
            headers = [header.strip() for header in reader.fieldnames]
            if len(set(headers)) != len(headers):
                raise ImportLogError(f"{source}: duplicate CSV column names")
            reader.fieldnames = headers
        if not reader.fieldnames or not {"Date", "Streamer", "Shop", "Start", "End"}.issubset(reader.fieldnames):
            raise ImportLogError(f"{source}: expected CSV columns Date, Streamer, Shop, Start, End")
        return import_rows(reader, source, db)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: python import_log.py STREAM-LOG.csv", file=sys.stderr)
        return 2
    try:
        init_db()
        with SessionLocal() as db:
            added, skipped = import_csv(args[0], db)
    except IntegrityError:
        print("Import failed: a record changed or conflicts with existing data. No rows were committed; retry after checking the file.", file=sys.stderr)
        return 1
    except (OSError, ImportLogError, csv.Error) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {added} slots, skipped {skipped} already there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
