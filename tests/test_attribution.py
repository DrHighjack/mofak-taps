import pytest
from datetime import datetime
from decimal import Decimal
from app import logic as L, models as M, attribution as A
from app.db import to_utc
from conftest import get

PT = lambda s: to_utc(datetime.fromisoformat(s))
HDR = "Order ID,Order Status,Created Time,Paid Time,SKU Subtotal After Discount,Shipping Fee After Discount,Taxes\n"


def csv_bytes(rows):
    return ("\ufeff" + HDR + "".join(rows)).encode()


def setup_slots(db):
    streamer_a, streamer_b, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Streamer, "Streamer B"), get(db, M.Shop, "Shop A")
    a = L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:00")); L.clock_out(db, streamer_a, shop_a, PT("2026-11-04T13:00"))
    b = L.clock_in(db, streamer_b, shop_a, PT("2026-11-04T13:00")); L.clock_out(db, streamer_b, shop_a, PT("2026-11-04T16:00"))
    return shop_a


def test_rules_boundaries_canceled_tabs_unassigned(db):
    shop_a = setup_slots(db)
    data = csv_bytes([
        "1001\t,Completed,11/04/2026 10:00:00 AM\t,11/04/2026 10:05:00 AM,12.50,1.50,0.00\n",   # inclusive start -> Streamer A
        "1002\t,Shipped,11/04/2026 12:59:59 PM\t,11/04/2026 01:10:00 PM,7.50,0.50,0.00\n",     # created in Streamer A, paid in Streamer B -> Streamer A
        "1003\t,Completed,11/04/2026 01:00:00 PM\t,,10.00,2.00,0.00\n",                          # exclusive end -> Streamer B
        "1004\t,Canceled,11/04/2026 01:30:00 PM\t,,99.00,9.00,0.00\n",                          # excluded
        "1005\t,Completed,11/04/2026 05:00:00 PM\t,,4.00,1.00,0.00\n",                           # outside -> Unassigned
    ])
    up = A.import_export(db, shop_a, datetime(2026, 11, 4).date(), "x.csv", data)
    assert up.rows == 5 and up.added == 5
    res = A.attribute(db, datetime(2026, 11, 4).date())
    labels = {l.label: l for l in res.lines}
    assert labels["Streamer A 10am-1pm"].orders == 2 and labels["Streamer A 10am-1pm"].product == Decimal("20.00") and labels["Streamer A 10am-1pm"].shipping == Decimal("2.00")
    assert labels["Streamer B 1pm-4pm"].orders == 1 and labels["Streamer B 1pm-4pm"].total == Decimal("12.00")
    assert labels["Unassigned (Shop A)"].orders == 1
    assert res.canceled == 1 and res.counted == 4
    t = res.total
    assert t.orders == 4 and t.product == Decimal("34.00") and t.shipping == Decimal("5.00")


def test_overlapping_slots_are_unassigned_with_explicit_reason_and_count(db):
    streamer_a, streamer_b, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Streamer, "Streamer B"), get(db, M.Shop, "Shop A")
    L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:00")); L.clock_out(db, streamer_a, shop_a, PT("2026-11-04T12:00"))
    L.clock_in(db, streamer_b, shop_a, PT("2026-11-04T11:00")); L.clock_out(db, streamer_b, shop_a, PT("2026-11-04T13:00"))
    A.import_export(db, shop_a, datetime(2026, 11, 4).date(), "overlap.csv",
                    csv_bytes(["1,Completed,11/04/2026 11:30:00 AM,,12.00,2.00,0\n"]))

    res = A.attribute(db, datetime(2026, 11, 4).date())
    overlap = next(line for line in res.lines if line.reason == "overlapping slots")
    assert overlap.label == "Unassigned (Shop A) - overlapping slots"
    assert overlap.orders == 1 and overlap.product == Decimal("12.00")
    assert res.overlap_count == 1 and res.total.orders == 1
    assert all(line.orders == 0 for line in res.lines if line.slot is not None)
    csv_result = A.result_csv(res)
    assert csv_result.splitlines()[0] == "Slot,Orders,Product sales,Shipping,Product + shipping"
    assert "Unassigned (Shop A) - overlapping slots" in csv_result


def test_reupload_does_not_double_count(db):
    shop_a = setup_slots(db)
    data = csv_bytes(["1,Completed,11/04/2026 10:30:00 AM,,20.00,3.00,0\n", "1,Completed,11/04/2026 10:30:00 AM,,20.00,3.00,0\n"])
    A.import_export(db, shop_a, datetime(2026, 11, 4).date(), "a.csv", data)
    up2 = A.import_export(db, shop_a, datetime(2026, 11, 4).date(), "a.csv", data)
    assert up2.added == 0
    res = A.attribute(db, datetime(2026, 11, 4).date())
    assert res.total.orders == 1 and res.total.product == Decimal("20.00")


def test_open_slot_prevents_silent_closed_slot_assignment(db):
    shop_a = setup_slots(db)
    streamer_c = get(db, M.Streamer, "Streamer C")
    L.clock_in(db, streamer_c, shop_a, PT("2026-11-04T12:00"))
    day = datetime(2026, 11, 4).date()
    A.import_export(db, shop_a, day, "open.csv", csv_bytes([
        "1,Completed,11/04/2026 11:30:00 AM,,10,1,0\n",
        "2,Completed,11/04/2026 12:30:00 PM,,20,2,0\n",
        "3,Completed,11/04/2026 01:30:00 PM,,30,3,0\n",
        "4,Completed,11/04/2026 05:30:00 PM,,40,4,0\n",
    ]))
    res = A.attribute(db, day)
    closed = [line for line in res.lines if line.slot is not None]
    assert [line.label for line in closed] == ["Streamer A 10am-1pm", "Streamer B 1pm-4pm"]
    assert [line.orders for line in closed] == [1, 0]
    reasons = {line.reason: line for line in res.lines if line.unassigned}
    assert reasons["overlapping slots"].orders == 2
    assert reasons["awaiting slot end"].orders == 1
    assert res.overlap_count == 2
    assert res.total.orders == 4 and res.total.product == Decimal("100.00")


def test_only_open_slot_keeps_sales_unassigned_awaiting_end(db):
    shop_a, streamer_a = get(db, M.Shop, "Shop A"), get(db, M.Streamer, "Streamer A")
    L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:00"))
    day = datetime(2026, 11, 4).date()
    A.import_export(db, shop_a, day, "open.csv", csv_bytes([
        "1,Completed,11/04/2026 10:00:00 AM,,10,1,0\n",
    ]))
    res = A.attribute(db, day)
    assert len(res.lines) == 1
    assert res.lines[0].reason == "awaiting slot end"
    assert res.lines[0].label == "Unassigned (Shop A) - awaiting slot end"
    assert res.lines[0].slot is None and res.lines[0].orders == 1
    assert res.overlap_count == 0


def test_same_streamer_two_slots_stay_two_rows(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T10:00")); L.clock_out(db, streamer_a, shop_a, PT("2026-11-04T11:00"))
    L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T14:00")); L.clock_out(db, streamer_a, shop_a, PT("2026-11-04T15:00"))
    A.import_export(db, shop_a, datetime(2026, 11, 4).date(), "a.csv",
                    csv_bytes(["1,Completed,11/04/2026 10:30:00 AM,,1.00,0,0\n", "2,Completed,11/04/2026 02:30:00 PM,,2.00,0,0\n"]))
    res = A.attribute(db, datetime(2026, 11, 4).date())
    assert [l.label for l in res.lines] == ["Streamer A 10am-11am", "Streamer A 2pm-3pm"]


def test_tab_separated_export_and_money_formats(db):
    shop_a = setup_slots(db)
    tsv = ("Order ID\tOrder Status\tCreated Time\tSKU Subtotal After Discount\tShipping Fee After Discount\n"
           "9\tCompleted\t2026-11-04 10:15:00\t$1,234.50\t$0.00\n").encode()
    A.import_export(db, shop_a, datetime(2026, 11, 4).date(), "a.csv", tsv)
    res = A.attribute(db, datetime(2026, 11, 4).date())
    assert res.lines[0].product == Decimal("1234.50")


def test_padded_tabs_do_not_override_comma_delimiter(db):
    shop_a = setup_slots(db)
    padded = csv_bytes([
        "\t9\t,\tCompleted\t,\t11/04/2026 10:15:00 AM\t,\t\t,\t$1234.50\t,\t$0.00\t,\t0.00\n"
    ])
    A.import_export(db, shop_a, datetime(2026, 11, 4).date(), "padded.csv", padded)
    res = A.attribute(db, datetime(2026, 11, 4).date())
    assert res.total.orders == 1
    assert res.total.product == Decimal("1234.50") and res.total.shipping == Decimal("0.00")


@pytest.mark.parametrize("value", [
    "not money", "NaN", "Infinity", "99999999999.00", "9999999999.994",
    "-9999999999.994", "9" * 100, "$$10",
])
def test_invalid_nonfinite_and_out_of_range_money_has_row_column_context(db, value):
    data = csv_bytes([f"9,Completed,11/04/2026 10:15:00 AM,,{value},0,0\n"])
    with pytest.raises(A.ImportError_, match=r"row 2, column 'SKU Subtotal After Discount'"):
        A.read_export(data)


def test_invalid_shipping_money_has_its_own_column_context():
    with pytest.raises(A.ImportError_, match="row 2, column 'Shipping Fee After Discount'"):
        A.read_export(csv_bytes(["9,Completed,2026-11-04 10:15:00,,1,bad,0\n"]))


def test_quoted_currency_and_field_size_boundaries_remain_valid():
    rows = A.read_export(csv_bytes([
        f'{"x" * 64},{"x" * 40},2026-11-04 10:15:00,,"$1,234.50",9999999999.99,0\n'
    ]))
    assert rows[0]["product"] == Decimal("1234.50")
    assert rows[0]["shipping"] == A.MAX_MONEY


@pytest.mark.parametrize("row", [
    "9,Completed,11/04/2026 10:15:00 AM,\n",
    "9,Completed,11/04/2026 10:15:00 AM,,1,0,0,unexpected\n",
])
def test_invalid_row_shapes_are_not_padded_or_truncated(row):
    with pytest.raises(A.ImportError_, match=r"Invalid CSV at row 2: expected 7 cells"):
        A.read_export(csv_bytes([row]))


def test_empty_money_cells_are_valid_and_trimmed_headers_are_supported():
    header = ",".join(f" \t{cell}\t " for cell in HDR.rstrip("\n").split(",")) + "\n"
    rows = A.read_export((header + " \t9\t , Completed ,2026-11-04 10:15:00,,, - ,0\n").encode())
    assert rows[0]["order_id"] == "9" and rows[0]["status"] == "Completed"
    assert rows[0]["product"] == 0 and rows[0]["shipping"] == 0


def test_duplicate_trimmed_headers_are_rejected():
    header = HDR.rstrip("\n") + ", \torder id\t \n"
    with pytest.raises(A.ImportError_, match="Duplicate trimmed headers at row 1"):
        A.read_export(header.encode())


def test_ambiguous_loose_header_matches_are_rejected():
    header = HDR.replace("Shipping Fee After Discount", "Shipping Fee After Discount A").rstrip("\n")
    header += ",Shipping Fee After Discount B\n"
    with pytest.raises(A.ImportError_, match="Header row 1: Ambiguous 'Shipping Fee After Discount'"):
        A.read_export(header.encode())


@pytest.mark.parametrize("data, message", [
    (HDR.encode() + b"9,Completed,\xff\n", "Invalid UTF-8 at row 2"),
    ((HDR + '9,"Completed,2026-11-04 10:15:00,,1,0,0\n').encode(), "Invalid CSV at row 2"),
    ((HDR + '9,"Completed"x,2026-11-04 10:15:00,,1,0,0\n').encode(), "Invalid CSV at row 2"),
])
def test_encoding_and_csv_syntax_errors_have_row_context(data, message):
    with pytest.raises(A.ImportError_, match=message):
        A.read_export(data)


@pytest.mark.parametrize("order_id,status,column", [
    ("x" * 65, "Completed", "Order ID"),
    ("9", "x" * 41, "Order Status"),
])
def test_order_fields_are_bounded_before_database_insert(order_id, status, column):
    with pytest.raises(A.ImportError_, match=f"row 2, column '{column}'"):
        A.read_export(csv_bytes([f"{order_id},{status},2026-11-04 10:15:00,,1,0,0\n"]))


@pytest.mark.parametrize("time,reason", [
    ("2026-03-08 02:30:00", "does not exist"),
    ("2026-11-01 01:30:00", "happens twice"),
    ("not-a-time", "Could not read the time"),
])
def test_created_time_validation_includes_row_and_column(time, reason):
    with pytest.raises(A.ImportError_, match=f"row 2, column 'Created Time':.*{reason}"):
        A.read_export(csv_bytes([f"9,Completed,{time},,1,0,0\n"]))


def test_parse_time_itself_rejects_ambiguous_pacific_time():
    with pytest.raises(A.ImportError_, match="happens twice"):
        A.parse_time("2026-11-01 01:30:00")


def test_errors_retain_physical_row_number_after_blank_lines():
    with pytest.raises(A.ImportError_, match="row 4, column 'SKU Subtotal After Discount'"):
        A.read_export(csv_bytes(["\n\n9,Completed,2026-11-04 10:15:00,,bad,0,0\n"]))


def test_same_order_id_in_different_shops_is_not_deduped_across_shops(db):
    shop_a = get(db, M.Shop, "Shop A")
    one_shop = get(db, M.Shop, "Shop B")
    selected_day = datetime(2026, 11, 4).date()
    data_a = csv_bytes(["same,Completed,11/04/2026 10:30:00 AM,,10.00,1.00,0\n"])
    data_b = csv_bytes(["same,Completed,11/04/2026 10:30:00 AM,,20.00,2.00,0\n"])
    A.import_export(db, shop_a, selected_day, "shop_a.csv", data_a)
    A.import_export(db, one_shop, selected_day, "one-shop.csv", data_b)
    res = A.attribute(db, selected_day)
    assert res.total.orders == 2
    assert res.total.product == Decimal("30.00") and res.total.shipping == Decimal("3.00")
    assert {line.shop for line in res.lines if line.unassigned} == {"Shop A", "Shop B"}


def test_selected_session_date_can_contain_next_day_crossmidnight_orders(db):
    streamer_a, shop_a = get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")
    L.clock_in(db, streamer_a, shop_a, PT("2026-11-04T23:00"))
    L.clock_out(db, streamer_a, shop_a, PT("2026-11-05T01:00"))
    selected_day = datetime(2026, 11, 4).date()
    A.import_export(db, shop_a, selected_day, "multi-day.csv", csv_bytes([
        "1,Completed,11/04/2026 11:30:00 PM,,10.00,1.00,0\n",
        "2,Completed,11/05/2026 12:30:00 AM,,20.00,2.00,0\n",
    ]))
    res = A.attribute(db, selected_day)
    assert res.total.orders == 2
    assert res.total.product == Decimal("30.00") and res.total.shipping == Decimal("3.00")
    assert res.lines[0].slot.day == selected_day and res.lines[0].orders == 2


def test_csv_output(db):
    shop_a = setup_slots(db)
    A.import_export(db, shop_a, datetime(2026, 11, 4).date(), "a.csv", csv_bytes(["1,Completed,11/04/2026 10:30:00 AM,,20.00,3.00,0\n"]))
    out = A.result_csv(A.attribute(db, datetime(2026, 11, 4).date()))
    assert out.splitlines()[0] == "Slot,Orders,Product sales,Shipping,Product + shipping"
    assert out.splitlines()[-1] == "Total,1,20.00,3.00,23.00"


def test_synthetic_export_is_attributed_across_three_slots(db):
    shop = get(db, M.Shop, "Shop A")
    for name, start, end in (
        ("Streamer A", "2026-11-04T10:00", "2026-11-04T11:00"),
        ("Streamer B", "2026-11-04T11:00", "2026-11-04T12:00"),
        ("Streamer C", "2026-11-04T12:00", "2026-11-04T13:00"),
    ):
        streamer = get(db, M.Streamer, name)
        L.clock_in(db, streamer, shop, PT(start))
        L.clock_out(db, streamer, shop, PT(end))
    data = csv_bytes([
        "1001,Completed,11/04/2026 10:15:00 AM,,12.00,1.00,0\n",
        "1002,Completed,11/04/2026 11:15:00 AM,,8.00,0.00,0\n",
        "1003,Completed,11/04/2026 12:15:00 PM,,15.00,2.00,0\n",
    ])
    day = datetime(2026, 11, 4).date()
    A.import_export(db, shop, day, "synthetic.csv", data)
    result = A.attribute(db, day)
    slot_lines = [line for line in result.lines if line.slot is not None]
    assert result.total_rows == result.counted == 3
    assert result.total.product == Decimal("35.00")
    assert result.total.shipping == Decimal("3.00")
    assert len(slot_lines) == 3
    assert all(line.orders == 1 for line in slot_lines)
