"""Create shops, streamers and tag links. Sample slots are optional."""
import argparse
import os
import secrets
from datetime import timedelta

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import select
from app.db import init_db, SessionLocal, now_utc, local_day
from app import models as M

STREAMERS = ["Streamer A", "Streamer B", "Streamer C", "Streamer D", "Streamer E", "Streamer F"]
SHOPS = ["Shop A", "Shop B"]


def seed(db, samples=False):
    shops, streamers = {}, {}
    for model, names, records in ((M.Shop, SHOPS, shops), (M.Streamer, STREAMERS, streamers)):
        for i, name in enumerate(names):
            item = db.scalars(select(model).where(model.name == name)).first()
            if item is None:
                item = model(name=name, sort=i)
                db.add(item)
                db.flush()
            records[name] = item
    if db.scalars(select(M.Token)).first() is None:
        db.add(M.Token(token=secrets.token_urlsafe(24), label="Shared box 1"))
    if samples and db.scalars(select(M.Slot)).first() is None:
        base = now_utc().replace(minute=0, second=0, microsecond=0) - timedelta(hours=9)
        for name, shop, first, last in [("Streamer A", "Shop A", 0, 3), ("Streamer B", "Shop A", 3, 6), ("Streamer C", "Shop B", 1, 5)]:
            start, end = base + timedelta(hours=first), base + timedelta(hours=last)
            db.add(M.Slot(day=local_day(start), streamer_id=streamers[name].id, shop_id=shops[shop].id,
                          start=start, end=end, status="closed", note="sample"))
    db.commit()
    return list(db.scalars(select(M.Token).where(M.Token.active.is_(True)).order_by(M.Token.id)))


def main():
    parser = argparse.ArgumentParser(description="Create the shops, names, and tap links.")
    parser.add_argument("--samples", action="store_true", help="Add three example slots to an empty slot log.")
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        tokens = seed(db, samples=args.samples)
        base = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
        print("Ready. Program these links onto the tags:")
        for tk in tokens:
            print(f"  {tk.label:20s} {base}/t/{tk.token}")


if __name__ == "__main__":
    main()
