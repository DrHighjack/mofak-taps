import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app import models as M
from app.db import SessionLocal, init_db
from test_web import ticket, remember


def setup_tags():
    with SessionLocal() as db:
        streamer = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one()
        shop = db.scalars(select(M.Shop).where(M.Shop.name == "Shop A")).one()
        first = M.Token(token="route-first", label="First box", streamer_id=streamer.id, shop_id=shop.id)
        second = M.Token(token="route-second", label="Second box", streamer_id=streamer.id)
        db.add_all([first, second])
        db.commit()
        return first.id, second.id, shop.id


def admin():
    client = TestClient(app)
    client.post("/admin/login", data={"password": "pw"})
    return client


def save(client, token_id, destination, custom_url=""):
    return client.post("/admin/tag-routing", data={
        "token_id": token_id, "destination": destination, "custom_url": custom_url,
    }, follow_redirects=False)


def test_routing_requires_admin_for_read_and_write():
    client = TestClient(app)
    assert client.get("/admin/tag-routing", follow_redirects=False).status_code == 303
    assert save(client, 0, "/day").status_code == 303
    with SessionLocal() as db:
        assert db.get(M.Setting, "tag_default_destination") is None


def test_default_preserves_assigned_flow_and_shared_destination_persists():
    first, second, _ = setup_tags()
    client = admin()
    assert "tapping?" in client.get("/t/route-first").text
    with SessionLocal() as db:
        person_id = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one().id
    remember(client, person_id, "route-first")
    assert "which shop" in client.get("/t/route-first").text
    assert "which shop" in client.get("/t/route-second").text
    assert save(client, 0, "/day").status_code == 303
    init_db()
    for token in ("route-first", "route-second"):
        response = client.get(f"/t/{token}", follow_redirects=False)
        assert response.status_code == 302 and response.headers["location"] == "/day"
        assert response.headers["cache-control"] == "no-store"
    with SessionLocal() as db:
        assert db.query(M.Slot).count() == 0
        assert db.query(M.Tap).filter(M.Tap.result == "redirect: /day").count() == 2
        audit = db.scalars(select(M.AuditLog).where(M.AuditLog.entity == "tag_route")).one()
        assert "assigned" in audit.before and "/day" in audit.after
    page = client.get("/admin/tag-routing")
    assert page.status_code == 200 and "First box" in page.text and "Current destination:" in page.text


def test_overrides_win_and_can_be_reset_to_inherit():
    first, second, _ = setup_tags()
    client = admin()
    with SessionLocal() as db:
        person_id = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one().id
    remember(client, person_id, "route-first")
    save(client, 0, "/day")
    save(client, first, "custom", "https://example.com/show?shop=one&source=nfc")
    save(client, second, "assigned")
    response = client.get("/t/route-first", follow_redirects=False)
    assert response.headers["location"] == "https://example.com/show?shop=one&source=nfc"
    assert "which shop" in client.get("/t/route-second").text
    save(client, 0, "/clock")
    assert client.get("/t/route-first", follow_redirects=False).headers["location"].startswith("https://example.com")
    save(client, first, "inherit")
    assert client.get("/t/route-first", follow_redirects=False).headers["location"] == "/clock"
    with SessionLocal() as db:
        assert db.get(M.TagRoute, first) is None
        assert db.get(M.TagRoute, second).destination == "assigned"


@pytest.mark.parametrize("destination,url", [
    ("custom", ""), ("custom", "javascript:alert(1)"), ("custom", "data:text/html,test"),
    ("custom", "//example.com"), ("custom", "/clock"), ("custom", "https://"),
    ("custom", "https://user:pw@example.com"), ("custom", "https://example.com:bad"),
    ("custom", "https://example.com/a b"), ("custom", "https://example.com\\evil"),
    ("custom", "https://example.com/\nheader"), ("custom", "https://example.com/t/route-first"),
    ("custom", "https://example.com/" + "a" * 200), ("/t/route-first", ""), ("inherit", ""),
])
def test_invalid_destinations_stay_on_menu_and_do_not_save(destination, url):
    setup_tags()
    client = admin()
    response = save(client, 0, destination, url)
    assert response.status_code == 400
    assert "NFC destinations" in response.text and 'role="alert"' in response.text
    assert "setInterval" not in response.text
    with SessionLocal() as db:
        assert db.get(M.Setting, "tag_default_destination") is None
        assert db.query(M.TagRoute).count() == 0


def test_disabled_and_unknown_tags_never_redirect_to_destination():
    first, _, _ = setup_tags()
    client = admin()
    save(client, 0, "custom", "https://example.com")
    with SessionLocal() as db:
        db.get(M.Token, first).active = False
        db.commit()
    for token in ("route-first", "unknown"):
        response = client.get(f"/t/{token}", follow_redirects=False)
        assert response.status_code == 200 and "location" not in response.headers
        assert "set up. Use the clock screen" in response.text


def test_changed_routing_blocks_old_confirmations_and_shop_forms():
    first, _, shop = setup_tags()
    client = admin()
    with SessionLocal() as db:
        person_id = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one().id
    remember(client, person_id, "route-first")
    pending = client.post("/t/route-first/shop", data={"shop_id": shop})
    save(client, first, "/day")
    response = client.post("/t/route-first/confirm", data={"ticket": ticket(pending)})
    assert "destination changed" in response.text
    for path in ("shop", "new"):
        response = client.post(f"/t/route-first/{path}", data={"shop_id": shop}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/t/route-first"
    response = client.get("/t/route-first/choose", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/t/route-first"
    with SessionLocal() as db:
        assert db.query(M.Slot).count() == 0


def test_protected_destination_does_not_grant_admin_access():
    setup_tags()
    save(admin(), 0, "/sales")
    public = TestClient(app)
    response = public.get("/t/route-first")
    assert response.url.path == "/admin/login"
    assert 'type="password"' in response.text


def test_unknown_tag_cannot_create_override():
    assert save(admin(), 999999, "/day").status_code == 404
