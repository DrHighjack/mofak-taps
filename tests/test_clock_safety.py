import html
import re
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import models as M, logic as L
from app.db import SessionLocal, to_utc, set_setting
from app.main import app
from conftest import get
from test_web import ticket, remember


def pt(value):
    return to_utc(datetime.fromisoformat(value))


def ids(db):
    return get(db, M.Streamer, "Streamer A"), get(db, M.Shop, "Shop A")


def make_tag(db, fixed=True):
    st, sh = ids(db)
    db.add(M.Token(token="safe-tag", streamer_id=st.id, shop_id=sh.id if fixed else None, label="Streamer A"))
    db.commit()
    return st, sh


def cancel_url(response):
    return html.unescape(re.search(r'href="([^"]+)"[^>]*>Cancel', response.text).group(1))


def admin():
    c = TestClient(app)
    c.post("/admin/login", data={"password": "pw"})
    return c


def test_no_slot_or_audit_until_confirm_and_cancel_invalidates(db):
    st, sh = make_tag(db)
    c = TestClient(app)
    remember(c, st.id, "safe-tag")
    question = c.post("/t/safe-tag/shop", data={"shop_id": sh.id})
    pending = ticket(question)
    assert db.query(M.Slot).count() == 0
    assert db.query(M.AuditLog).count() == 0
    r = c.get(cancel_url(question))
    assert "Nothing was changed" in r.text and 'name="ticket"' not in r.text
    r = c.post("/t/safe-tag/confirm", data={"ticket": pending})
    assert "already used or canceled" in r.text
    assert db.query(M.Slot).count() == 0
    assert db.scalars(select(M.Tap).where(M.Tap.result == "canceled")).first()


def test_confirm_takes_current_server_time_and_cannot_replay(db):
    st, sh = make_tag(db)
    c = TestClient(app)
    remember(c, st.id, "safe-tag")
    q = c.post("/t/safe-tag/shop", data={"shop_id": sh.id})
    assert "set-cookie" not in q.headers and c.cookies
    with patch("app.logic.now_utc", return_value=pt("2026-11-04T10:02:31")):
        r = c.post("/t/safe-tag/confirm", data={"ticket": ticket(q)})
    assert "clocked in" in r.text and "/clock/done" in r.text
    assert db.scalars(select(M.Slot)).one().start == pt("2026-11-04T10:02:31")
    assert 'name="ticket"' not in c.get("/clock/done").text
    c.post("/t/safe-tag/confirm", data={"ticket": ticket(q)})
    assert db.query(M.Slot).count() == 1


def test_direct_or_tampered_confirm_cannot_write(db):
    make_tag(db)
    c = TestClient(app)
    c.post("/t/safe-tag/confirm", data={"streamer_id": 1, "shop_id": 1, "action": "in"})
    c.post("/clock/confirm", data={"ticket": "tampered"})
    assert db.query(M.Slot).count() == 0


def test_refused_confirm_remains_in_tap_history(db):
    st, sh = make_tag(db)
    c = TestClient(app)
    remember(c, st.id, "safe-tag")
    pending = ticket(c.post("/t/safe-tag/shop", data={"shop_id": sh.id}))
    L.clock_in(db, st, sh)
    db.commit()
    r = c.post("/t/safe-tag/confirm", data={"ticket": pending})
    assert "already clocked in" in r.text
    assert db.scalars(select(M.Tap).where(M.Tap.result.like("refused:%"))).first()


def test_legacy_fixed_shop_is_ignored_and_revoked_token_cannot_confirm(db):
    st, sh = make_tag(db)
    c = TestClient(app)
    remember(c, st.id, "safe-tag")
    pending = ticket(c.post("/t/safe-tag/shop", data={"shop_id": sh.id}))
    other = get(db, M.Shop, "Shop B")
    assert "Clock in Streamer A on Shop B" in c.post("/t/safe-tag/shop", data={"shop_id": other.id}).text
    tk = db.scalars(select(M.Token)).one()
    tk.active = False
    db.commit()
    assert "turned off" in c.post("/t/safe-tag/confirm", data={"ticket": pending}).text
    assert db.query(M.Slot).count() == 0
    refused = db.scalars(select(M.Tap).where(M.Tap.result.like("refused:%")).order_by(M.Tap.id.desc())).first()
    assert refused.streamer == "Streamer A" and refused.shop == "Shop A"


def test_rate_limit_applies_to_confirmation_and_is_logged(db):
    make_tag(db)
    c = TestClient(app)
    for _ in range(31):
        response = c.post("/t/safe-tag/confirm", data={"ticket": "invalid"})
    assert "Too many requests" in response.text
    assert db.scalars(select(M.Tap).where(M.Tap.result.like("%Too many requests%"))).first()


def test_kiosk_write_rate_limit(db):
    c = TestClient(app)
    for _ in range(61):
        response = c.post("/clock/confirm", data={"ticket": "invalid"})
    assert "Too many requests" in response.text


def test_late_clock_out_replaces_auto_end_once_and_keeps_review_flag(db):
    st, sh = ids(db)
    slot = L.clock_in(db, st, sh, pt("2026-11-04T10:00"))
    L.auto_close(db, pt("2026-11-04T11:00"))
    out = L.clock_out(db, st, sh, pt("2026-11-04T13:30"))
    assert out.id == slot.id and L.slot_hours(out) == 3.5
    assert out.status == "auto_closed" and out.note == "late clock-out, needs review"
    assert L.late_slot(db, st, sh) is None
    assert db.query(M.Slot).count() == 1
    db.flush()
    entry = db.scalars(select(M.AuditLog).where(M.AuditLog.action == "late_clock_out")).one()
    assert "19:00" in entry.before and "21:30" in entry.after


def test_late_tap_offers_correction_or_new_stream(db):
    st, sh = make_tag(db)
    L.clock_in(db, st, sh, pt("2026-11-04T10:00"))
    L.auto_close(db, pt("2026-11-04T11:00"))
    db.commit()
    c = TestClient(app)
    remember(c, st.id, "safe-tag")
    q = c.post("/t/safe-tag/shop", data={"shop_id": sh.id})
    assert "Clock out now instead" in q.text and "Starting a new stream?" in q.text
    with patch("app.logic.now_utc", return_value=pt("2026-11-04T13:00")):
        r = c.post("/t/safe-tag/confirm", data={"ticket": ticket(q)})
    assert "automatic end was replaced" in r.text
    assert "Clock in Streamer A" in c.post("/t/safe-tag/shop", data={"shop_id": sh.id}).text


def test_clock_in_after_auto_close_creates_new_slot(db):
    st, sh = ids(db)
    previous = L.clock_in(db, st, sh, pt("2026-11-04T10:00"))
    L.auto_close(db, pt("2026-11-04T11:00"))
    new = L.clock_in(db, st, sh, pt("2026-11-04T14:00"))
    assert new.id != previous.id
    assert L.late_slot(db, st, sh) is None


def test_database_rejects_duplicate_open_and_negative_hours(db):
    st, sh = ids(db)
    L.clock_in(db, st, sh)
    db.commit()
    db.add(M.Slot(day=datetime(2026, 11, 4).date(), streamer_id=st.id, shop_id=sh.id,
                  start=pt("2026-11-04T10:00"), end=None))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    st, sh = ids(db)
    db.add(M.Slot(day=datetime(2026, 11, 4).date(), streamer_id=st.id, shop_id=sh.id,
                  start=pt("2026-11-04T10:00"), end=pt("2026-11-04T09:00")))
    with pytest.raises(IntegrityError):
        db.flush()


def test_admin_negative_end_hidden_selection_and_audit(db):
    st, sh = ids(db)
    slot = L.clock_in(db, st, sh, pt("2026-11-04T10:00"))
    L.clock_out(db, st, sh, pt("2026-11-04T13:00"))
    st.active = False
    db.commit()
    c = admin()
    r = c.get(f"/admin/slot/{slot.id}")
    assert f'<option value="{st.id}" selected>Streamer A</option>' in r.text
    r = c.post(f"/admin/slot/{slot.id}", data={"streamer_id": st.id, "shop_id": sh.id,
                                           "start": "2026-11-04T13:00", "end": "2026-11-04T10:00"})
    assert r.status_code == 400
    db.refresh(slot)
    assert L.slot_hours(slot) == 3
    slot.status = "auto_closed"
    db.commit()
    c.post(f"/admin/slot/{slot.id}/reviewed")
    db.refresh(slot)
    assert slot.note == "reviewed"
    assert db.scalars(select(M.AuditLog).where(M.AuditLog.action == "reviewed")).one().before


def test_admin_shared_tag_label_edit_preserves_link_and_legacy_metadata(db):
    st, sh = make_tag(db)
    tk = db.scalars(select(M.Token)).one()
    c = admin()
    c.post("/admin/tags", data={"edit_id": tk.id, "label": "Station two", "shop_id": ""})
    db.refresh(tk)
    assert tk.label == "Station two" and tk.shop_id == sh.id
    assert tk.token == "safe-tag"
    assert "tapping?" in TestClient(app).get("/t/safe-tag").text


def test_hidden_tag_refused_and_admin_timer_validation(db):
    st, sh = make_tag(db)
    st.active = False
    db.commit()
    assert "tapping?" in TestClient(app).get("/t/safe-tag").text
    c = admin()
    assert c.post("/admin/settings", data={"auto_close_hours": "-1"}).status_code == 400
    assert c.post("/admin/settings", data={"auto_close_hours": "1.5"}).status_code == 400


def test_plain_history_and_slot_log_date_range(db):
    st, sh = ids(db)
    s = L.clock_in(db, st, sh, pt("2026-11-04T10:00"))
    L.clock_out(db, st, sh, pt("2026-11-04T13:00"))
    db.commit()
    c = admin()
    r = c.get("/admin/log")
    assert "Streamer:</b> Streamer A" in r.text and '"streamer_id"' not in r.text
    assert "Streamer A" in c.get("/admin/slots?start=2026-11-04&end=2026-11-04").text
    assert "Streamer A" not in c.get("/log.csv?start=2026-11-05&end=2026-11-05").text
    assert "Streamer A" in c.get("/log.csv?start=2026-11-04&end=2026-11-04").text


def test_scheduled_late_out_and_custom_hours(db):
    st, sh = ids(db)
    set_setting(db, "auto_close_hours", "2")
    s = L.clock_in(db, st, sh, pt("2026-11-04T10:00"))
    assert L.auto_close(db, pt("2026-11-04T11:30")) == []
    assert L.auto_close(db, pt("2026-11-04T12:00")) == [s]
    L.clock_out(db, st, sh, pt("2026-11-04T12:30"))
    assert L.slot_hours(s) == 2.5


def test_smart_tag_never_guesses_from_open_slot_or_history(db):
    st, sh = make_tag(db, fixed=False)
    L.clock_in(db, st, sh)
    db.commit()
    c = TestClient(app)
    remember(c, st.id, "safe-tag")
    assert "which shop" in c.get("/t/safe-tag").text


def test_change_selection_invalidates_pending_confirmation(db):
    st, sh = make_tag(db, fixed=False)
    c = TestClient(app)
    remember(c, st.id, "safe-tag")
    q = c.post("/t/safe-tag/shop", data={"shop_id": sh.id})
    pending = ticket(q)
    c.get(cancel_url(q) + "&change=true")
    assert "already used or canceled" in c.post("/t/safe-tag/confirm", data={"ticket": pending}).text


def test_stale_clock_out_cannot_close_a_new_slot(db):
    st, sh = make_tag(db)
    L.clock_in(db, st, sh)
    db.commit()
    c = TestClient(app)
    remember(c, st.id, "safe-tag")
    pending = ticket(c.post("/t/safe-tag/shop", data={"shop_id": sh.id}))
    L.clock_out(db, st, sh)
    L.clock_in(db, st, sh)
    db.commit()
    assert "slot changed" in c.post("/t/safe-tag/confirm", data={"ticket": pending}).text
    assert L.open_slot(db, st, sh)


def test_daylight_saving_clock_hours(db):
    st, sh = ids(db)
    # UTC timestamps span the repeated Pacific hour on November 1.
    start, end = datetime(2026, 11, 1, 8, 30), datetime(2026, 11, 1, 10, 30)
    s = L.clock_in(db, st, sh, start)
    L.clock_out(db, st, sh, end)
    assert L.slot_hours(s) == 2


def test_upload_persists_result_and_reupload_updates_without_duplicates(db):
    st, sh = ids(db)
    L.clock_in(db, st, sh, pt("2026-11-04T10:00"))
    L.clock_out(db, st, sh, pt("2026-11-04T13:00"))
    db.commit()
    c = admin()
    header = "Order ID,Order Status,Created Time,SKU Subtotal After Discount,Shipping Fee After Discount\n"
    data = (header + "1,Completed,11/04/2026 10:30:00 AM,20.00,3.00\n").encode()
    r = c.post("/sales/upload", data={"day": "2026-11-04", "shop_id": sh.id},
               files={"file": ("sample.csv", data, "text/csv")})
    assert r.status_code == 200 and "$20.00" in r.text
    c.post("/sales/upload", data={"day": "2026-11-04", "shop_id": sh.id},
           files={"file": ("sample.csv", data.replace(b"Completed", b"Canceled"), "text/csv")})
    assert db.query(M.Order).count() == 1
    assert db.query(M.Upload).count() == 2
    runs = list(db.scalars(select(M.AttributionRun).order_by(M.AttributionRun.id)))
    assert len(runs) == 2
    assert "Total,1,20.00,3.00,23.00" in runs[0].result
    assert "Total,0,0.00,0.00,0.00" in runs[1].result


def test_invalid_upload_rolls_back_all_rows_and_shows_error(db):
    st, sh = ids(db)
    db.commit()
    data = (b"Order ID,Order Status,Created Time,SKU Subtotal After Discount,Shipping Fee After Discount\n"
            b"1,Completed,11/04/2026 10:30:00 AM,20.00,3.00\n"
            b"2,Completed,11/04/2026 10:30:00 AM,not-money,3.00\n")
    response = admin().post("/sales/upload", data={"day": "2026-11-04", "shop_id": sh.id},
                            files={"file": ("bad.csv", data, "text/csv")})
    assert "Couldn" in response.text and "row 3" in response.text
    assert db.query(M.Order).count() == db.query(M.Upload).count() == db.query(M.AttributionRun).count() == 0


def test_manual_missed_slot_can_be_added_and_is_audited(db):
    st, sh = ids(db)
    c = admin()
    assert "Add a missed slot" in c.get("/admin/slots/new").text
    r = c.post("/admin/slots/new", data={"streamer_id": st.id, "shop_id": sh.id,
                                        "start": "2026-11-04T22:00", "end": "2026-11-05T01:00"})
    assert r.status_code == 200
    s = db.scalars(select(M.Slot)).one()
    assert L.slot_hours(s) == 3 and str(s.day) == "2026-11-04"
    assert s.status == "estimated"
    assert db.scalars(select(M.AuditLog)).one().after


def test_sheets_failure_is_explicit_and_never_redirects_as_success(db):
    from app import sheets as S
    c = admin()
    with patch.object(S, "sync", side_effect=S.SheetsError("Check the sharing permission.")):
        r = c.post("/sheets/sync", data={"day": "2026-11-04", "back": "/day"})
    assert r.status_code == 502 and "Your slots are safe" in r.text
    assert "Check the sharing permission" in r.text


def test_sales_screen_and_csv_filter_the_same_shop(db):
    from app import attribution as A
    st, sh = ids(db)
    other = get(db, M.Shop, "Shop B")
    for shop, amount in ((sh, "20"), (other, "50")):
        L.clock_in(db, st, shop, pt("2026-11-04T10:00"))
        L.clock_out(db, st, shop, pt("2026-11-04T13:00"))
        data = (f"Order ID,Order Status,Created Time,SKU Subtotal After Discount,Shipping Fee After Discount\n"
                f"1,Completed,11/04/2026 10:30:00 AM,{amount},3\n").encode()
        A.import_export(db, shop, datetime(2026, 11, 4).date(), "shop.csv", data)
    db.commit()
    c = admin()
    for shop, amount in ((sh, "20"), (other, "50")):
        query = f"?date=2026-11-04&shop_id={shop.id}"
        assert f"${amount}.00" in c.get("/sales" + query).text
        csv = c.get("/sales.csv" + query).text
        assert csv.splitlines()[0] == "Slot,Orders,Product sales,Shipping,Product + shipping"
        assert f"Total,1,{amount}.00,3.00,{int(amount) + 3}.00" in csv
    assert c.get("/sales?shop_id=999").status_code == 400
