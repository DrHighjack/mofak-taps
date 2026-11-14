from fastapi.testclient import TestClient
from app.main import app
from app import models as M
from app.db import SessionLocal
from sqlalchemy import select
import re
import html


def ticket(response):
    match = re.search(r'name="ticket" value="([^"]+)"', response.text)
    assert match, response.text
    return html.unescape(match.group(1))


def client():
    return TestClient(app)


def remember(c, streamer_id, token):
    response = c.post(f"/t/{token}/person", data={"streamer_id": streamer_id}, follow_redirects=False)
    assert response.status_code == 303
    return response


def test_kiosk_flow_and_refusals():
    c = client()
    r = c.post("/clock/ask", data={"streamer_id": "", "shop_id": "", "action": "in"})
    assert "Pick your name first" in r.text
    with SessionLocal() as db:
        streamer_a = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one().id
        shop_a = db.scalars(select(M.Shop).where(M.Shop.name == "Shop A")).one().id
    r = c.post("/clock/ask", data={"streamer_id": streamer_a, "shop_id": shop_a, "action": "in"})
    assert "Clock in Streamer A on Shop A at" in r.text and "Confirm" in r.text
    r = c.post("/clock/confirm", data={"ticket": ticket(r)})
    assert "Streamer A clocked in on Shop A" in r.text
    r = c.post("/clock/ask", data={"streamer_id": streamer_a, "shop_id": shop_a, "action": "in"})
    assert "already clocked in" in r.text and "Confirm" not in r.text
    assert "Streamer A" in c.get("/clock").text and "since" in c.get("/clock").text


def test_tap_flow():
    with SessionLocal() as db:
        streamer_a = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one()
        db.add(M.Token(token="abc123", streamer_id=streamer_a.id, label="Streamer A box")); db.commit()
    c = client()
    assert "Who&#39;s tapping?" in c.get("/t/abc123").text or "Who's tapping?" in c.get("/t/abc123").text
    remember(c, streamer_a.id, "abc123")
    assert "which shop" in c.get("/t/abc123").text
    with SessionLocal() as db:
        shop_a = db.scalars(select(M.Shop).where(M.Shop.name == "Shop A")).one().id
    r = c.post("/t/abc123/shop", data={"shop_id": shop_a})
    assert "Clock in Streamer A on Shop A" in r.text
    with SessionLocal() as db:
        eli_id = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one().id
    r = c.post("/t/abc123/confirm", data={"ticket": ticket(r)})
    assert "clocked in" in r.text
    r = c.post("/t/abc123/shop", data={"shop_id": shop_a})
    assert "Clock out Streamer A on Shop A" in r.text
    assert "Use the clock screen" in c.get("/t/nope").text


def test_admin_requires_login_and_day_csv():
    c = client()
    assert c.get("/sales", follow_redirects=False).status_code == 303
    c.post("/admin/login", data={"password": "pw", "next": "/admin"})
    assert "Admin" in c.get("/admin").text
    r = c.get("/day.csv")
    assert r.headers["content-type"].startswith("text/csv") and "Paste line" in r.text
    assert c.get("/health").text == "ok"


def test_navigation_groups_admin_tools_without_redundant_tabs():
    c = client()
    public = c.get("/clock").text.split('<nav class="top"', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r'href="([^"]+)"', public) == ["/clock", "/day", "/admin/login"]
    c.post("/admin/login", data={"password": "pw"})
    for path in ("/clock", "/admin", "/admin/tags", "/admin/tag-routing", "/admin/settings"):
        page = c.get(path).text
        header = page.split('<nav class="top"', 1)[1].split("</nav>", 1)[0]
        assert re.findall(r'href="([^"]+)"', header) == ["/clock", "/day", "/sales", "/admin"]
        if path.startswith("/admin"):
            assert 'href="/admin" class="on"' in header
    overview = c.get("/admin").text
    tools = overview.split('aria-label="Admin tools"', 1)[1].split("</nav>", 1)[0]
    for path in ("/admin/slots", "/admin/log", "/admin/people", "/admin/tags", "/admin/schedule", "/admin/settings"):
        assert f'href="{path}"' in tools
    assert 'href="/admin/logout"' in overview
    assert 'href="/admin/tag-routing"' in c.get("/admin/tags").text


def test_kiosk_errors_stay_on_form_without_timer_and_preserve_choices():
    c = client()
    with SessionLocal() as db:
        streamer_a = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one().id
        shop_a = db.scalars(select(M.Shop).where(M.Shop.name == "Shop A")).one().id
    r = c.post("/clock/ask", data={"streamer_id": streamer_a, "shop_id": "", "action": "in"})
    assert 'id="clock-form"' in r.text and "Pick your shop first" in r.text
    assert f'<option value="{streamer_a}" selected>Streamer A</option>' in r.text
    assert "location.href" not in r.text and "setInterval" not in r.text
    assert 'id="streamer_id" required' in r.text and 'id="shop_id" required' in r.text
    pending = c.post("/clock/ask", data={"streamer_id": streamer_a, "shop_id": shop_a, "action": "in"})
    c.post("/clock/confirm", data={"ticket": ticket(pending)})
    r = c.post("/clock/ask", data={"streamer_id": streamer_a, "shop_id": shop_a, "action": "in"})
    assert 'id="clock-form"' in r.text and "already clocked in" in r.text
    assert f'<option value="{shop_a}" selected>Shop A</option>' in r.text
    assert "location.href" not in r.text


def test_kiosk_availability_blocks_invalid_actions_without_writing():
    c = client()
    with SessionLocal() as db:
        streamer_a = db.scalars(select(M.Streamer).where(M.Streamer.name == "Streamer A")).one().id
        shop_a = db.scalars(select(M.Shop).where(M.Shop.name == "Shop A")).one().id
    url = f"/clock/check?streamer_id={streamer_a}&shop_id={shop_a}"
    checks = c.get(url).json()
    assert checks["in"] == "" and checks["out"] == "Streamer A is not clocked in on Shop A"
    with SessionLocal() as db:
        assert db.query(M.Slot).count() == db.query(M.Confirmation).count() == 0
    pending = c.post("/clock/ask", data={"streamer_id": streamer_a, "shop_id": shop_a, "action": "in"})
    c.post("/clock/confirm", data={"ticket": ticket(pending)})
    checks = c.get(url).json()
    assert "already clocked in" in checks["in"] and checks["out"] == ""
    r = c.post("/clock/confirm", data={"ticket": ticket(pending)})
    assert "already used" in r.text and 'id="clock-form"' in r.text
    assert "location.href" not in r.text
