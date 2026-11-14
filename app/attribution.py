"""TikTok Seller Center order export -> orders table -> per-slot sales.

Rules (from the PRD):
- Assign each order to a slot by Created Time, not Paid Time. Export times are Pacific.
- Exclude orders whose Order Status is Canceled.
- Product sales = "SKU Subtotal After Discount". Shipping = "Shipping Fee After Discount". Never taxes.
- Strip trailing tabs from Order ID and time columns (the export pads them).
- One row per order; count distinct Order ID.
- Slot boundary inclusive of start, exclusive of end.
- Orders outside every slot are reported as Unassigned, never dropped.
- Re-uploading the same file must not double count (unique on shop + day + order id).
"""
import csv, io, re
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Order, Upload, Slot, Shop
from .db import to_utc, now_utc, audit, parse_local_input
from .logic import slot_label, slots_for_day, scheduled_for_day

TIME_FORMATS = ["%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M %p",
                "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"]
MAX_MONEY = Decimal("9999999999.99")


class ImportError_(Exception):
    pass


def _clean(s: str) -> str:
    return (s or "").replace("\ufeff", "").strip().strip("\t").strip()


def _money(s: str, row_number: int, column: str) -> Decimal:
    s = _clean(s)
    if s in ("", "-"):
        return Decimal("0")
    if not re.fullmatch(r"[+-]?\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", s):
        raise ImportError_(f"Invalid money at row {row_number}, column '{column}': {s!r}.")
    try:
        amount = Decimal(s.replace("$", "").replace(",", ""))
        if not amount.is_finite() or abs(amount) > MAX_MONEY:
            raise ImportError_(f"Money is out of range at row {row_number}, column '{column}': {s!r}.")
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ImportError_(f"Invalid money at row {row_number}, column '{column}': {s!r}.") from exc
    return amount


def parse_time(s: str) -> datetime:
    s = _clean(s)
    for f in TIME_FORMATS:
        try:
            local = datetime.strptime(s, f)
        except ValueError:
            continue
        try:
            parse_local_input(local.isoformat())
        except ValueError as exc:
            raise ImportError_(str(exc)) from exc
        return local
    raise ImportError_(f"Could not read the time '{s}'. Expected something like 11/04/2026 10:15:23 AM.")


def _find_col(headers: list[str], *wanted: str) -> str:
    low = {h.lower(): h for h in headers}
    for w in wanted:
        if w.lower() in low:
            return low[w.lower()]
    for w in wanted:                       # loose match: all words present
        words = w.lower().split()
        matches = [h for h in headers if all(x in h.lower() for x in words)]
        if len(matches) > 1:
            raise ImportError_(f"Ambiguous '{w}' column: {', '.join(matches)}.")
        if matches:
            return matches[0]
    raise ImportError_(f"The file has no '{wanted[0]}' column. Columns found: {', '.join(headers[:12])}")


def read_export(data: bytes) -> list[dict]:
    """Returns one dict per order row: order_id, status, created (naive UTC), product, shipping."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        prefix = data[:exc.start].replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        row_number = prefix.count(b"\n") + 1
        raise ImportError_(f"Invalid UTF-8 at row {row_number}. Save the export as UTF-8.") from exc
    candidates = []
    for delimiter in (",", "\t"):
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        parsed, syntax_error = [], None
        try:
            while True:
                row_number = reader.line_num + 1
                row = next(reader)
                if any(cell.strip() for cell in row):
                    parsed.append((row_number, row))
        except StopIteration:
            pass
        except csv.Error as exc:
            syntax_error = ImportError_(f"Invalid CSV at row {row_number}: {exc}.")
        header_index = next((i for i, (_, row) in enumerate(parsed)
                             if any("order id" in _clean(cell).lower() for cell in row)), None)
        if header_index is None:
            candidates.append(((-1, -1), parsed, None, syntax_error))
            continue
        headers = [_clean(cell) for cell in parsed[header_index][1]]
        targets = ("Order ID", "Order Status", "Created Time",
                   "SKU Subtotal After Discount", "Shipping Fee After Discount")
        exact = sum(1 for target in targets if any(header.lower() == target.lower() for header in headers))
        score = sum(1 for target in targets if _has_col(headers, target))
        candidates.append(((exact, score), parsed, header_index, syntax_error))
    _, rows, hi, syntax_error = max(candidates, key=lambda candidate: candidate[0])
    if syntax_error:
        raise syntax_error
    if not rows:
        raise ImportError_("The file is empty.")
    if hi is None:
        raise ImportError_("Could not find the header row (no 'Order ID' column).")
    header_row, raw_headers = rows[hi]
    headers = [_clean(h) for h in raw_headers]
    if len({h.lower() for h in headers}) != len(headers):
        raise ImportError_(f"Duplicate trimmed headers at row {header_row}.")
    try:
        c_id = _find_col(headers, "Order ID")
        c_status = _find_col(headers, "Order Status")
        c_created = _find_col(headers, "Created Time")
        c_prod = _find_col(headers, "SKU Subtotal After Discount")
        c_ship = _find_col(headers, "Shipping Fee After Discount")
    except ImportError_ as exc:
        raise ImportError_(f"Header row {header_row}: {exc}") from exc
    idx = {h: i for i, h in enumerate(headers)}
    out = []
    for row_number, r in rows[hi + 1:]:
        if len(r) != len(headers):
            raise ImportError_(f"Invalid CSV at row {row_number}: expected {len(headers)} cells, found {len(r)}.")
        oid = _clean(r[idx[c_id]])
        if not oid:
            continue
        status = _clean(r[idx[c_status]])
        for value, limit, column in ((oid, 64, c_id), (status, 40, c_status)):
            if len(value) > limit:
                raise ImportError_(f"Value too long at row {row_number}, column '{column}' (maximum {limit} characters).")
        try:
            created_local = parse_time(r[idx[c_created]])
        except ImportError_ as exc:
            raise ImportError_(f"Invalid time at row {row_number}, column '{c_created}': {exc}") from exc
        out.append({
            "order_id": oid,
            "status": status,
            "created": to_utc(created_local),
            "product": _money(r[idx[c_prod]], row_number, c_prod),
            "shipping": _money(r[idx[c_ship]], row_number, c_ship),
        })
    return out


def _has_col(headers: list[str], wanted: str) -> bool:
    wanted = wanted.lower()
    return any(header.lower() == wanted or
               (len(header.split()) <= len(wanted.split()) + 1 and
                all(word in header.lower() for word in wanted.split()))
               for header in headers)


def import_export(db: Session, shop: Shop, day: date, filename: str, data: bytes, who="Admin") -> Upload:
    rows = read_export(data)
    up = Upload(shop_id=shop.id, day=day, filename=filename, uploaded_at=now_utc(), rows=len(rows), added=0)
    db.add(up)
    db.flush()
    existing = {o.order_id: o for o in db.scalars(select(Order).where(Order.shop_id == shop.id, Order.day == day))}
    seen = set()
    added = 0
    for r in rows:
        if r["order_id"] in seen:
            continue                      # same order twice in one file: count once
        seen.add(r["order_id"])
        o = existing.get(r["order_id"])
        if o:                             # re-upload: refresh values, never duplicate
            o.status, o.created, o.product, o.shipping = r["status"], r["created"], r["product"], r["shipping"]
            continue
        db.add(Order(upload_id=up.id, shop_id=shop.id, day=day, order_id=r["order_id"], status=r["status"],
                     created=r["created"], product=r["product"], shipping=r["shipping"]))
        added += 1
    up.added = added
    audit(db, who, "import", "upload", up.id, after={"shop": shop.name, "day": str(day), "file": filename, "rows": len(rows), "added": added})
    return up


# ---- attribution --------------------------------------------------------------
@dataclass
class Line:
    label: str
    orders: int = 0
    product: Decimal = Decimal("0")
    shipping: Decimal = Decimal("0")
    slot: Slot | None = None
    estimated: bool = False
    unassigned: bool = False
    shop: str = ""
    reason: str = ""

    @property
    def total(self) -> Decimal:
        return self.product + self.shipping


@dataclass
class Result:
    lines: list[Line] = field(default_factory=list)
    counted: int = 0
    canceled: int = 0
    total_rows: int = 0
    overlap_count: int = 0

    @property
    def total(self) -> Line:
        t = Line("Total")
        for l in self.lines:
            t.orders += l.orders
            t.product += l.product
            t.shipping += l.shipping
        return t


def is_canceled(status: str) -> bool:
    return status.strip().lower().startswith("cancel")


def attribute(db: Session, day: date, shop: Shop | None = None) -> Result:
    """Per-slot table for a day (optionally one shop). Slots in time order; Unassigned last."""
    all_slots = [s for s in slots_for_day(db, day) if shop is None or s.shop_id == shop.id]
    slots = [s for s in all_slots if s.end is not None]
    q = select(Order).where(Order.day == day)
    if shop is not None:
        q = q.where(Order.shop_id == shop.id)
    orders = list(db.scalars(q))
    res = Result(total_rows=len(orders))
    lines = {s.id: Line(slot_label(s) + (" (est.)" if s.status == "estimated" else ""), slot=s,
                        estimated=(s.status == "estimated"), shop=s.shop.name) for s in slots}
    unassigned: dict[tuple[int, str], Line] = {}
    for o in orders:
        if is_canceled(o.status):
            res.canceled += 1
            continue
        res.counted += 1
        hits = [s for s in all_slots if s.shop_id == o.shop_id and s.start <= o.created
                and (s.end is None or o.created < s.end)]
        if len(hits) > 1:
            shop_name = db.get(Shop, o.shop_id).name
            reason = "overlapping slots"
            line = unassigned.setdefault(
                (o.shop_id, reason),
                Line(f"Unassigned ({shop_name}) - {reason}", unassigned=True, shop=shop_name, reason=reason))
            res.overlap_count += 1
        elif hits and hits[0].end is None:
            shop_name = db.get(Shop, o.shop_id).name
            reason = "awaiting slot end"
            line = unassigned.setdefault(
                (o.shop_id, reason),
                Line(f"Unassigned ({shop_name}) - {reason}", unassigned=True, shop=shop_name, reason=reason))
        elif not hits:
            shop_name = db.get(Shop, o.shop_id).name
            line = unassigned.setdefault(
                (o.shop_id, ""),
                Line(f"Unassigned ({shop_name})", unassigned=True, shop=shop_name))
        else:
            line = lines[hits[0].id]
        line.orders += 1
        line.product += Decimal(o.product)
        line.shipping += Decimal(o.shipping)
    res.lines = [lines[s.id] for s in slots] + list(unassigned.values())
    return res


def gaps_with_schedule(db: Session, day: date) -> list:
    """Scheduled slots for the day that no logged slot covers: offered as 'Use schedule for this gap'."""
    slots = slots_for_day(db, day)
    out = []
    for s in scheduled_for_day(db, day):
        covered = any(sl.streamer_id == s.streamer.id and sl.shop_id == s.shop.id and sl.start < s.end
                      and (sl.end or now_utc()) > s.start for sl in slots)
        if not covered:
            out.append(s)
    return out


def result_csv(res: Result) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Slot", "Orders", "Product sales", "Shipping", "Product + shipping"])
    for l in res.lines:
        w.writerow([l.label, l.orders, f"{l.product:.2f}", f"{l.shipping:.2f}", f"{l.total:.2f}"])
    t = res.total
    w.writerow(["Total", t.orders, f"{t.product:.2f}", f"{t.shipping:.2f}", f"{t.total:.2f}"])
    return buf.getvalue()
