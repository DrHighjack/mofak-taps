import os, json
from pathlib import Path
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, select, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, Session
from .models import Base, Setting, DEFAULT_SETTINGS, AuditLog, Slot, User, Token

TZ = ZoneInfo(os.environ.get("APP_TZ", "America/Los_Angeles"))
UTC = ZoneInfo("UTC")

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/streamclock.db")
_engine_kwargs = {"connect_args": {"check_same_thread": False}} if DATABASE_URL.startswith("sqlite") else {"pool_pre_ping": True}
engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    url = make_url(DATABASE_URL)
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    if not next(c for c in inspect(engine).get_columns("tokens") if c["name"] == "streamer_id")["nullable"]:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE tokens ALTER COLUMN streamer_id DROP NOT NULL"))
                conn.commit()
            elif engine.dialect.name == "sqlite":
                foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
                conn.commit()
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
                conn.commit()
                try:
                    with conn.begin():
                        conn.exec_driver_sql("""CREATE TABLE tokens_shared (
                            id INTEGER PRIMARY KEY, token VARCHAR(40) NOT NULL UNIQUE,
                            label VARCHAR(120) NOT NULL, streamer_id INTEGER REFERENCES streamers(id),
                            shop_id INTEGER REFERENCES shops(id), active BOOLEAN NOT NULL)""")
                        conn.exec_driver_sql("INSERT INTO tokens_shared SELECT id, token, label, streamer_id, shop_id, active FROM tokens")
                        conn.exec_driver_sql("DROP TABLE tokens")
                        conn.exec_driver_sql("ALTER TABLE tokens_shared RENAME TO tokens")
                finally:
                    conn.exec_driver_sql(f"PRAGMA foreign_keys={int(foreign_keys)}")
                    conn.commit()
            else:
                raise RuntimeError("Shared tag migration supports PostgreSQL and SQLite only.")
    for index in Slot.__table__.indexes:
        index.create(engine, checkfirst=True)
    with SessionLocal() as db:
        for k, v in DEFAULT_SETTINGS.items():
            if db.get(Setting, k) is None:
                db.add(Setting(key=k, value=v))
        if db.scalars(select(User).where(User.name == "Admin")).first() is None:
            db.add(User(name="Admin"))
        if db.get(Setting, "clock_rules_version") is None:
            previous = db.get(Setting, "auto_close_hours")
            if previous and previous.value == "6":
                previous.value = "1"
            db.add(Setting(key="clock_rules_version", value="2"))
        if db.get(Setting, "shared_boxes_version") is None:
            for tag in db.scalars(select(Token)):
                if tag.streamer and tag.label == f"{tag.streamer.name}'s box":
                    before = {"label": tag.label}
                    tag.label = f"Shared box {tag.id}"
                    audit(db, "system", "shared_box_conversion", "token", tag.id,
                          before=before, after={"label": tag.label})
            db.add(Setting(key="shared_boxes_version", value="1"))
        db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- time helpers: DB stores naive UTC; people see Pacific -----------------
def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(TZ)


def to_utc(local: datetime) -> datetime:
    if local.tzinfo is None:
        local = local.replace(tzinfo=TZ)
    return local.astimezone(UTC).replace(tzinfo=None)


def parse_local_input(value: str) -> datetime:
    local = datetime.fromisoformat(value)
    if local.tzinfo is not None:
        raise ValueError("Enter the time shown on the Pacific clock, without a time zone.")
    converted = to_utc(local)
    if to_local(converted).replace(tzinfo=None) != local:
        raise ValueError("That Pacific time does not exist because the clocks change. Pick another time.")
    if local.replace(tzinfo=TZ, fold=0).utcoffset() != local.replace(tzinfo=TZ, fold=1).utcoffset():
        raise ValueError("That Pacific time happens twice when the clocks change. Ask Admin to check the time.")
    return converted


def local_day(dt: datetime) -> date:
    return to_local(dt).date()


def fmt_time(dt: datetime | None) -> str:
    """h:mm AM/PM, Pacific."""
    if dt is None:
        return ""
    l = to_local(dt)
    return l.strftime("%I:%M %p").lstrip("0")


def fmt_time_short(dt: datetime | None) -> str:
    """10:02am / 1pm  (slot-label style)."""
    if dt is None:
        return ""
    l = to_local(dt)
    h = l.strftime("%I").lstrip("0")
    ampm = l.strftime("%p").lower()
    return f"{h}{ampm}" if l.minute == 0 else f"{h}:{l.strftime('%M')}{ampm}"


def fmt_paste_time(dt: datetime | None) -> str:
    """10:02AM (paste-line style from the sheet)."""
    if dt is None:
        return ""
    return to_local(dt).strftime("%I:%M%p").lstrip("0")


def local_dt(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=TZ)


# ---- settings ---------------------------------------------------------------
def get_setting(db: Session, key: str) -> str:
    s = db.get(Setting, key)
    return s.value if s else DEFAULT_SETTINGS.get(key, "")


def set_setting(db: Session, key: str, value: str):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))


def setting_int(db: Session, key: str) -> int:
    try:
        return int(float(get_setting(db, key)))
    except ValueError:
        return int(DEFAULT_SETTINGS[key])


# ---- audit ------------------------------------------------------------------
def audit(db: Session, who: str, action: str, entity: str, entity_id: int | None, before=None, after=None):
    db.add(AuditLog(at=now_utc(), who=who, action=action, entity=entity, entity_id=entity_id,
                    before=json.dumps(before, default=str) if before is not None else "",
                    after=json.dumps(after, default=str) if after is not None else ""))
