from datetime import datetime, date
from sqlalchemy import (String, Integer, Boolean, DateTime, Date, Text, Numeric,
                        ForeignKey, UniqueConstraint, Index, CheckConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Streamer(Base):
    __tablename__ = "streamers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Shop(Base):
    __tablename__ = "shops"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Token(Base):
    """One per physical tap box. The tag's URL is /t/<token>."""
    __tablename__ = "tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(40), unique=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    # Legacy assignments are preserved as metadata, never used for shared-box identity or shop selection.
    streamer_id: Mapped[int | None] = mapped_column(ForeignKey("streamers.id"), nullable=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    streamer: Mapped[Streamer | None] = relationship()
    shop: Mapped[Shop | None] = relationship()


class Tap(Base):
    __tablename__ = "taps"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime)          # naive UTC
    token: Mapped[str] = mapped_column(String(40))
    streamer: Mapped[str] = mapped_column(String(80), default="")
    shop: Mapped[str] = mapped_column(String(80), default="")
    result: Mapped[str] = mapped_column(String(200), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class TagRoute(Base):
    __tablename__ = "tag_routes"
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), primary_key=True)
    destination: Mapped[str] = mapped_column(String(200))


class Slot(Base):
    __tablename__ = "slots"
    __table_args__ = (
        CheckConstraint('"end" IS NULL OR "end" >= start', name="valid_slot_times"),
        Index("one_open_slot_per_shop", "streamer_id", "shop_id", unique=True,
              postgresql_where=text('"end" IS NULL'), sqlite_where=text('"end" IS NULL')),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)                 # clock-in date, Pacific
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    start: Mapped[datetime] = mapped_column(DateTime)       # naive UTC
    end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open / closed / auto_closed / estimated
    note: Mapped[str] = mapped_column(String(200), default="")
    streamer: Mapped[Streamer] = relationship()
    shop: Mapped[Shop] = relationship()


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    day: Mapped[date] = mapped_column(Date)
    filename: Mapped[str] = mapped_column(String(200))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    shop: Mapped[Shop] = relationship()


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("shop_id", "day", "order_id", name="uq_order"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    day: Mapped[date] = mapped_column(Date)
    order_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="")
    created: Mapped[datetime] = mapped_column(DateTime)     # naive UTC
    product: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    shipping: Mapped[float] = mapped_column(Numeric(12, 2), default=0)


class ScheduleRule(Base):
    """Weekly repeating pattern. weekday: 0=Mon .. 6=Sun. Times are Pacific HH:MM."""
    __tablename__ = "schedule_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    weekday: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[str] = mapped_column(String(5))
    end_time: Mapped[str] = mapped_column(String(5))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    streamer: Mapped[Streamer] = relationship()
    shop: Mapped[Shop] = relationship()


class ScheduleOverride(Base):
    """One-off change for a date. kind: add / cancel."""
    __tablename__ = "schedule_overrides"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    start_time: Mapped[str] = mapped_column(String(5))
    end_time: Mapped[str] = mapped_column(String(5))
    kind: Mapped[str] = mapped_column(String(10), default="add")
    streamer: Mapped[Streamer] = relationship()
    shop: Mapped[Shop] = relationship()


class Dismissal(Base):
    """A no-show flag Admin dismissed; never shown again."""
    __tablename__ = "dismissals"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    streamer_id: Mapped[int] = mapped_column(Integer)
    shop_id: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[str] = mapped_column(String(5))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    who: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before: Mapped[str] = mapped_column(Text, default="")
    after: Mapped[str] = mapped_column(Text, default="")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)


class AttributionRun(Base):
    __tablename__ = "attribution_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    at: Mapped[datetime] = mapped_column(DateTime)
    who: Mapped[str] = mapped_column(String(80))
    result: Mapped[str] = mapped_column(Text)


class Confirmation(Base):
    __tablename__ = "confirmations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires: Mapped[datetime] = mapped_column(DateTime)


class RateLimit(Base):
    __tablename__ = "rate_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    bucket: Mapped[int] = mapped_column(Integer)
    hits: Mapped[int] = mapped_column(Integer)


DEFAULT_SETTINGS = {
    "auto_close_hours": "1",        # fallback when no schedule matches
    "auto_close_grace_min": "30",   # minutes past scheduled end before auto-close
    "noshow_after_min": "30",       # minutes after scheduled start before a no-show flag
    "smart_default_before_min": "60",  # tap this long before a scheduled start skips the shop picker
    "allow_both_shops": "1",        # streamer may be open on both shops at once
    "message_seconds": "5",
    "last_sheets_sync": "",
}
