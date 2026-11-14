from unittest.mock import patch
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select, create_engine, text, inspect

from app import models as M, db as D
from app.main import app, PERSON_COOKIE, PERSON_MAX_AGE
from itsdangerous import SignatureExpired
from test_web import remember, ticket
from conftest import get


def boxes(db):
    streamer_a = get(db, M.Streamer, "Streamer A")
    streamer_b = get(db, M.Streamer, "Streamer B")
    shop = get(db, M.Shop, "Shop A")
    db.add_all([
        M.Token(token="shared-a", label="Old Streamer A box", streamer_id=streamer_a.id, shop_id=shop.id),
        M.Token(token="shared-b", label="Old Streamer B box", streamer_id=streamer_b.id),
    ])
    db.commit()
    return streamer_a, streamer_b, shop


def test_two_phones_use_any_box_without_changing_each_others_identity(db):
    streamer_a, streamer_b, shop = boxes(db)
    first, second = TestClient(app), TestClient(app)
    assert "tapping?" in first.get("/t/shared-a").text
    assert "tapping?" in second.get("/t/shared-a").text
    remember(first, streamer_a.id, "shared-a")
    remember(second, streamer_b.id, "shared-b")
    for token in ("shared-a", "shared-b"):
        assert "Streamer A, which shop?" in first.get(f"/t/{token}").text
        assert "Streamer B, which shop?" in second.get(f"/t/{token}").text
    pending = first.post("/t/shared-b/shop", data={"shop_id": shop.id})
    assert "Clock in Streamer A on Shop A" in pending.text
    assert db.query(M.Slot).count() == 0
    first.post("/t/shared-b/confirm", data={"ticket": ticket(pending)})
    slot = db.scalars(select(M.Slot)).one()
    assert slot.streamer_id == streamer_a.id
    db.expire_all()
    assert db.scalars(select(M.Tap).where(M.Tap.result == "success: in")).one().streamer == "Streamer A"
    assert "Clock out Streamer A" in first.post("/t/shared-a/shop", data={"shop_id": shop.id}).text


def test_schedule_uses_phone_identity_not_legacy_box_assignment(db):
    streamer_a, streamer_b, shop = boxes(db)
    client = TestClient(app)
    remember(client, streamer_b.id, "shared-a")
    with patch("app.logic.scheduled_shop_now", return_value=shop) as scheduled:
        page = client.get("/t/shared-a")
    assert "Clock in Streamer B on Shop A" in page.text
    assert scheduled.call_args.args[1].id == streamer_b.id
    with patch("app.logic.scheduled_shop_now", return_value=None):
        assert "Streamer B, which shop?" in client.get("/t/shared-a").text
    assert db.query(M.Slot).count() == 0


def test_real_schedule_selects_shop_and_conflicting_shops_ask(db):
    streamer_a, streamer_b, shop = boxes(db)
    other = get(db, M.Shop, "Shop B")
    at = D.to_utc(datetime(2026, 11, 4, 10, 30))
    db.add(M.ScheduleRule(streamer_id=streamer_b.id, shop_id=other.id, weekday=2,
                          start_time="10:00", end_time="13:00"))
    db.commit()
    client = TestClient(app)
    remember(client, streamer_b.id, "shared-a")
    with patch("app.logic.now_utc", return_value=at):
        assert "Clock in Streamer B on Shop B" in client.get("/t/shared-a").text
        db.add(M.ScheduleRule(streamer_id=streamer_b.id, shop_id=shop.id, weekday=2,
                              start_time="10:00", end_time="13:00"))
        db.commit()
        assert "Streamer B, which shop?" in client.get("/t/shared-a").text


def test_person_cannot_change_without_signout_and_old_ticket_stays_invalid(db):
    streamer_a, streamer_b, shop = boxes(db)
    client = TestClient(app)
    remember(client, streamer_a.id, "shared-a")
    pending = ticket(client.post("/t/shared-a/shop", data={"shop_id": shop.id}))
    old_cookie = client.cookies.get(PERSON_COOKIE)
    remember(client, streamer_b.id, "shared-b")
    assert client.cookies.get(PERSON_COOKIE) == old_cookie
    assert "Streamer A, which shop?" in client.get("/t/shared-b").text
    client.post("/t/shared-b/signout")
    assert not client.cookies.get(PERSON_COOKIE)
    assert "tapping?" in client.get("/t/shared-a").text
    response = client.post("/t/shared-a/confirm", data={"ticket": pending})
    assert "signed out" in response.text
    remember(client, streamer_a.id, "shared-b")
    assert client.cookies.get(PERSON_COOKIE) != old_cookie
    assert "signed out" in client.post("/t/shared-a/confirm", data={"ticket": pending}).text
    assert db.query(M.Slot).count() == 0


def test_confirmation_is_bound_to_phone_and_person(db):
    streamer_a, streamer_b, shop = boxes(db)
    first, second = TestClient(app), TestClient(app)
    remember(first, streamer_a.id, "shared-a")
    remember(second, streamer_b.id, "shared-b")
    pending = ticket(first.post("/t/shared-a/shop", data={"shop_id": shop.id}))
    assert "remembered person changed" in second.post("/t/shared-a/confirm", data={"ticket": pending}).text
    second.post("/t/shared-b/signout")
    remember(second, streamer_a.id, "shared-b")
    assert "remembered person changed" in second.post("/t/shared-a/confirm", data={"ticket": pending}).text
    assert db.query(M.Slot).count() == 0
    assert "clocked in" in first.post("/t/shared-a/confirm", data={"ticket": pending}).text


def test_cookie_attributes_expiry_tampering_hidden_person_and_kiosk(db):
    streamer_a, _, _ = boxes(db)
    client = TestClient(app, base_url="https://testserver")
    response = remember(client, streamer_a.id, "shared-a")
    cookie = response.headers["set-cookie"]
    assert f"Max-Age={PERSON_MAX_AGE}" in cookie and "HttpOnly" in cookie
    assert "SameSite=lax" in cookie and "Secure" in cookie and "Path=/" in cookie
    kiosk = client.get("/clock").text
    assert 'id="streamer_id"' in kiosk and "Pick your name first" in kiosk
    with patch("app.main.person_signer.loads", side_effect=SignatureExpired("expired")):
        assert "tapping?" in client.get("/t/shared-a").text
    streamer_a.active = False
    db.commit()
    assert "tapping?" in client.get("/t/shared-a").text
    assert "Streamer A</option>" not in client.get("/t/shared-a").text
    client.cookies.clear()
    client.cookies.set(PERSON_COOKIE, "tampered")
    assert "tapping?" in client.get("/t/shared-a").text


def test_new_boxes_need_no_person_and_seed_does_not_duplicate_legacy_boxes(db):
    boxes(db)
    client = TestClient(app)
    client.post("/admin/login", data={"password": "pw"})
    assert client.post("/admin/tags", data={"label": "Reception"}).status_code == 200
    db.expire_all()
    token = db.scalars(select(M.Token).where(M.Token.label == "Reception")).one()
    assert token.streamer_id is None and token.shop_id is None
    from seed import seed
    before = db.query(M.Token).count()
    seed(db)
    assert db.query(M.Token).count() == before
    assert "tapping?" in client.get(f"/t/{token.token}").text


def test_invalid_person_stays_inline_and_missing_cookies_are_explained(db):
    boxes(db)
    client = TestClient(app)
    for value in ("", "nope", "999999"):
        response = client.post("/t/shared-a/person", data={"streamer_id": value})
        assert "Pick your name from the list." in response.text
        assert "set-cookie" not in response.headers and "setInterval" not in response.text
    assert "Allow cookies" in client.get("/t/shared-a?remembered=1").text
    assert client.post("/t/shared-a/shop", data={"shop_id": 1}, follow_redirects=False).status_code == 303


def test_sqlite_upgrade_preserves_old_links_routes_and_history(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("""CREATE TABLE tokens (
            id INTEGER PRIMARY KEY, token VARCHAR(40) NOT NULL UNIQUE,
            label VARCHAR(120) NOT NULL, streamer_id INTEGER NOT NULL,
            shop_id INTEGER, active BOOLEAN NOT NULL)"""))
        conn.execute(text("INSERT INTO tokens VALUES (5, 'existing-link', 'Old box', 1, 2, 1)"))
    monkeypatch.setattr(D, "engine", engine)
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(D, "SessionLocal", sessionmaker(bind=engine))
    D.init_db()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tag_routes VALUES (5, '/day')"))
    D.init_db()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT token, streamer_id, shop_id FROM tokens WHERE id=5")).one() == ("existing-link", 1, 2)
        assert conn.execute(text("SELECT destination FROM tag_routes WHERE token_id=5")).scalar() == "/day"
        assert next(c for c in inspect(conn).get_columns("tokens") if c["name"] == "streamer_id")["nullable"]
    engine.dispose()


def test_conversion_renames_only_old_generated_labels_and_runs_once(db):
    streamer_a, _, _ = boxes(db)
    first = db.scalars(select(M.Token).where(M.Token.token == "shared-a")).one()
    second = db.scalars(select(M.Token).where(M.Token.token == "shared-b")).one()
    first.label = "Streamer A's box"
    second.label = "Reception"
    db.delete(db.get(M.Setting, "shared_boxes_version"))
    db.commit()
    D.init_db()
    db.expire_all()
    assert first.label == f"Shared box {first.id}" and second.label == "Reception"
    assert first.token == "shared-a" and first.streamer_id == streamer_a.id
    before = db.query(M.AuditLog).count()
    D.init_db()
    assert db.query(M.AuditLog).count() == before
