import os, secrets, threading, time, hmac, hashlib, json, logging
from urllib.parse import urlencode, urlsplit
from datetime import date, datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import select, desc, delete, case
from sqlalchemy.exc import IntegrityError
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from . import models as M
from .db import (init_db, get_db, SessionLocal, now_utc, to_local, to_utc, local_day, fmt_time,
                 get_setting, set_setting, setting_int, audit, TZ, DEFAULT_SETTINGS, parse_local_input)
from . import logic as L
from . import attribution as A
from . import sheets as S

BASE = Path(__file__).parent
app = FastAPI(title="Stream clock")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals.update(fmt_time=fmt_time, to_local=to_local, slot_hours=L.slot_hours,
                             paste_line=L.paste_line, slot_label=L.slot_label)

SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
signer = URLSafeTimedSerializer(SECRET, salt="admin")
clock_signer = URLSafeTimedSerializer(SECRET, salt="clock")
person_signer = URLSafeTimedSerializer(SECRET, salt="remembered-person")
PERSON_COOKIE = "streamclock_person"
PERSON_MAX_AGE = 365 * 24 * 60 * 60
logger = logging.getLogger("streamclock")

TAG_DESTINATIONS = {
    "assigned": "Shared clock flow - remember person on their phone",
    "/clock": "Clock - name and shop pickers",
    "/day": "Today",
    "/sales": "Sales by slot (admin login required)",
    "/admin": "Admin (login required)",
}


def tag_destination(db, tk):
    override = db.get(M.TagRoute, tk.id)
    return override.destination if override else (get_setting(db, "tag_default_destination") or "assigned")


def validate_tag_destination(choice, custom_url):
    if choice in TAG_DESTINATIONS or choice == "inherit":
        return choice
    if choice != "custom":
        raise ValueError("Choose a destination from the list.")
    value = custom_url.strip()
    if not value or len(value) > 200 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Enter a website URL with no spaces and no more than 200 characters.")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError
        parsed.port
    except ValueError:
        raise ValueError("Enter a full http:// or https:// website URL without a username or password.")
    if parsed.path.startswith("/t/"):
        raise ValueError("Choose a page, not another tag link, to avoid a redirect loop.")
    return value


@app.on_event("startup")
def _startup():
    if not ADMIN_PASSWORD or ADMIN_PASSWORD in ("changeme", "change-me"):
        raise RuntimeError("Set ADMIN_PASSWORD in .env before starting.")
    if not os.environ.get("SECRET_KEY") or SECRET.startswith("change-me"):
        raise RuntimeError("Set a random SECRET_KEY in .env before starting.")
    init_db()
    if os.environ.get("DISABLE_JOBS") != "1":
        threading.Thread(target=_jobs, daemon=True).start()


def _jobs():
    """Every minute: auto-close. Once a night (2:10am Pacific): Sheets sync."""
    last_sync_day = None
    while True:
        try:
            with SessionLocal() as db:
                L.auto_close(db)
                db.commit()
                now = to_local(now_utc())
                if (now.hour, now.minute) >= (2, 10) and last_sync_day != now.date() and S.configured():
                    S.sync(db, now.date() - timedelta(days=1))
                    last_sync_day = now.date()
        except Exception:
            logger.exception("Scheduled clock maintenance or Sheets sync failed")
        time.sleep(60)


# ---- helpers ------------------------------------------------------------------
def render(request: Request, name: str, **ctx):
    ctx.setdefault("request", request)
    ctx.setdefault("msg_seconds", 5)
    ctx.setdefault("is_admin", is_admin(request))
    return templates.TemplateResponse(request, name, ctx)


@app.middleware("http")
async def protect_forms(request: Request, call_next):
    origin = request.headers.get("origin")
    allowed = {str(request.base_url).rstrip("/"), os.environ.get("PUBLIC_URL", "").rstrip("/")}
    if request.method == "POST" and origin and origin not in allowed:
        return PlainTextResponse("Open this page on the clock site and try again.", status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(RequestValidationError)
async def bad_form(request: Request, exc: RequestValidationError):
    logger.warning("Invalid form at %s; fields: %s", request.url.path, [error["loc"] for error in exc.errors()])
    return PlainTextResponse("Some details are missing or unreadable. Go back and check the form.", status_code=422)


@app.exception_handler(L.ClockError)
async def clock_error(request: Request, exc: L.ClockError):
    return PlainTextResponse(str(exc), status_code=400)


@app.exception_handler(HTTPException)
async def user_error(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return templates.TemplateResponse(request, "error.html", {"detail": exc.detail, "is_admin": is_admin(request)},
                                      status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(IntegrityError)
async def conflict(request: Request, exc: IntegrityError):
    logger.warning("Conflicting database update at %s", request.url.path, exc_info=exc)
    return PlainTextResponse("That change conflicts with an existing record. Go back, refresh, and check it.", status_code=409)


def is_admin(request: Request) -> bool:
    c = request.cookies.get("admin")
    if not c:
        return False
    try:
        return signer.loads(c, max_age=60 * 60 * 24 * 30) == hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
    except BadSignature:
        return False


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": f"/admin/login?next={request.url.path}"})


def active_streamers(db: Session):
    return list(db.scalars(select(M.Streamer).where(M.Streamer.active.is_(True)).order_by(M.Streamer.sort, M.Streamer.name)))


def active_shops(db: Session):
    return list(db.scalars(select(M.Shop).where(M.Shop.active.is_(True)).order_by(M.Shop.sort, M.Shop.name)))


def parse_day(s: str | None) -> date:
    if not s:
        return to_local(now_utc()).date()
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, "Choose a valid date.")


def rate_ok(key: str, limit=30, window=60) -> bool:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    bucket = int(time.time()) // window
    digest = hashlib.sha256(key.encode()).hexdigest()
    with SessionLocal() as db:
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        stmt = insert(M.RateLimit).values(key=digest, bucket=bucket, hits=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"bucket": bucket, "hits": case(
                (M.RateLimit.bucket == bucket, M.RateLimit.hits + 1), else_=1)}
        ).returning(M.RateLimit.hits)
        hits = db.scalar(stmt)
        db.execute(delete(M.RateLimit).where(M.RateLimit.bucket < bucket - 2))
        db.commit()
        return hits <= limit


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_allowed(request: Request, token: str = "") -> bool:
    ip_ok = rate_ok(f"clock-ip:{client_ip(request)}", limit=60)
    token_ok = rate_ok(f"clock-token:{token}", limit=30) if token else True
    return ip_ok and token_ok


def redirect_message(path: str, msg: str):
    return RedirectResponse(path + ("&" if "?" in path else "?") + urlencode({"msg": msg}), status_code=303)


def person_shop(db, streamer_id, shop_id):
    st, sh = db.get(M.Streamer, streamer_id), db.get(M.Shop, shop_id)
    if st is None or sh is None:
        raise L.ClockError("That name or shop was not found. Pick again.")
    return st, sh


def sales_shops(db, shop_id):
    shops = list(db.scalars(select(M.Shop).order_by(M.Shop.sort, M.Shop.name)))
    selected = next((s for s in shops if s.id == shop_id), None) if shop_id else (shops[0] if shops else None)
    if shop_id and selected is None:
        raise HTTPException(400, "That shop was not found. Choose another shop.")
    return shops, selected


def tap_record(db, request, token, result, st=None, sh=None):
    if st is None:
        st = remembered_person(request, db)
    db.add(M.Tap(at=now_utc(), token=token[:40], streamer=st.name if st else "",
                 shop=sh.name if sh else "", result=result[:200], ip=client_ip(request)))
    db.commit()


def remembered_person(request, db):
    cookie = request.cookies.get(PERSON_COOKIE)
    if not cookie:
        return None
    try:
        payload = person_signer.loads(cookie, max_age=PERSON_MAX_AGE)
    except BadSignature:
        logger.info("Expired or invalid remembered-person cookie")
        return None
    person = db.get(M.Streamer, payload["id"])
    return person if person and person.active else None


def tap_person_page(request, db, token, error=""):
    return render(request, "tap_person.html", token=token, streamers=active_streamers(db), error=error)


def question(request, db, st, sh, action, token=""):
    target = L.check_clock(db, st, sh, action)
    if action == "out" and target.status == "auto_closed":
        action = "late_out"
    nonce = secrets.token_urlsafe(24)
    db.execute(delete(M.Confirmation).where(M.Confirmation.expires < now_utc()))
    db.add(M.Confirmation(id=nonce, expires=now_utc() + timedelta(minutes=10)))
    db.commit()
    ticket = clock_signer.dumps({"id": nonce, "streamer": st.id, "shop": sh.id, "action": action,
                                "token": token, "target": target.id if target else None,
                                "target_end": str(target.end) if target else None,
                                "device": hashlib.sha256(request.cookies.get(PERSON_COOKIE, "").encode()).hexdigest() if token else ""})
    return render(request, "question.html", streamer=st, shop=sh, action=action,
                  when=fmt_time(now_utc()), ticket=ticket,
                  confirm_url=f"/t/{token}/confirm" if token else "/clock/confirm",
                  cancel_url="/clock/cancel?" + urlencode({"ticket": ticket}),
                  change_url=f"/t/{token}/choose" if token else "/clock", tap=bool(token), token=token)


# ---- health ------------------------------------------------------------------
@app.get("/health", response_class=PlainTextResponse)
def health(db: Session = Depends(get_db)):
    db.execute(select(M.Setting).limit(1))
    return "ok"


@app.get("/")
def root():
    return RedirectResponse("/clock")


# ---- kiosk ---------------------------------------------------------------------
@app.get("/clock", response_class=HTMLResponse)
def clock(request: Request, db: Session = Depends(get_db)):
    return clock_form(request, db)


def clock_form(request, db, error="", streamer_id="", shop_id=""):
    return render(request, "clock.html", streamers=active_streamers(db), shops=active_shops(db),
                  open_slots=L.open_slots(db), error=error,
                  selected_streamer=str(streamer_id), selected_shop=str(shop_id))


@app.get("/clock/check")
def clock_check(streamer_id: int, shop_id: int, db: Session = Depends(get_db)):
    errors = {}
    for action in ("in", "out"):
        try:
            st, sh = person_shop(db, streamer_id, shop_id)
            L.check_clock(db, st, sh, action)
            errors[action] = ""
        except L.ClockError as exc:
            errors[action] = str(exc)
    return errors


@app.post("/clock/ask", response_class=HTMLResponse)
def clock_ask(request: Request, streamer_id: str = Form(""), shop_id: str = Form(""), action: str = Form(...),
              db: Session = Depends(get_db)):
    if not rate_allowed(request):
        return clock_form(request, db, "Too many requests. Wait a minute and try again.", streamer_id, shop_id)
    if not streamer_id:
        return clock_form(request, db, "Pick your name first", streamer_id, shop_id)
    if not shop_id:
        return clock_form(request, db, "Pick your shop first", streamer_id, shop_id)
    try:
        st, sh = person_shop(db, int(streamer_id), int(shop_id))
        return question(request, db, st, sh, action)
    except ValueError:
        return clock_form(request, db, "Pick your name and shop from the list.", streamer_id, shop_id)
    except L.ClockError as e:
        db.rollback()
        return clock_form(request, db, str(e), streamer_id, shop_id)


@app.post("/clock/confirm", response_class=HTMLResponse)
def clock_confirm(request: Request, ticket: str = Form(""), db: Session = Depends(get_db)):
    return _do_clock(request, db, ticket)


@app.get("/clock/cancel")
def cancel(request: Request, ticket: str = "", change: bool = False, db: Session = Depends(get_db)):
    destination = "/clock"
    try:
        payload = clock_signer.loads(ticket, max_age=600)
        db.execute(delete(M.Confirmation).where(M.Confirmation.id == payload["id"]))
        db.commit()
        if payload["token"]:
            st, sh = person_shop(db, payload["streamer"], payload["shop"])
            tap_record(db, request, payload["token"], "canceled", st, sh)
            destination = f"/t/{payload['token']}/choose"
    except BadSignature:
        logger.info("Expired or invalid cancel link")
    if change:
        return RedirectResponse(destination, status_code=303)
    return render(request, "done.html", msg="Canceled. Nothing was changed.")


@app.get("/clock/time", response_class=PlainTextResponse)
def server_time():
    return fmt_time(now_utc())


@app.get("/clock/done", response_class=HTMLResponse)
def done(request: Request):
    return render(request, "done.html", msg="All done. You can close this page.")


def _do_clock(request, db, ticket, token=""):
    st = sh = None
    result = ""
    if not rate_allowed(request, token):
        result = "Too many requests. Wait a minute and try again."
    try:
        if result:
            raise L.ClockError(result)
        try:
            payload = clock_signer.loads(ticket, max_age=600)
        except BadSignature:
            raise L.ClockError("This question expired. Open the clock or tap again.")
        if payload["token"] != token:
            raise L.ClockError("This question belongs to another tag. Tap again.")
        st, sh = person_shop(db, payload["streamer"], payload["shop"])
        if token:
            tk = db.scalars(select(M.Token).where(M.Token.token == token, M.Token.active.is_(True))
                            .with_for_update()).first()
            if not tk:
                raise L.ClockError("This tag changed or was turned off. Use the clock screen.")
            person = remembered_person(request, db)
            device = hashlib.sha256(request.cookies.get(PERSON_COOKIE, "").encode()).hexdigest()
            if not person or person.id != st.id or payload.get("device") != device:
                raise L.ClockError("The remembered person changed or signed out. Tap the box again.")
            if tag_destination(db, tk) != "assigned":
                raise L.ClockError("This tag's destination changed. Tap the tag again.")
        nonce = db.scalar(delete(M.Confirmation).where(
            M.Confirmation.id == payload["id"], M.Confirmation.expires >= now_utc())
                          .returning(M.Confirmation.id))
        if nonce is None:
            raise L.ClockError("This question was already used or canceled. Tap again or open the clock.")
        L.lock_streamer(db, st.id)
        target = L.check_clock(db, st, sh, payload["action"])
        if ((target.id if target else None) != payload["target"]
                or (str(target.end) if target else None) != payload["target_end"]):
            raise L.ClockError("This slot changed while you were confirming. Tap again or open the clock.")
        who = f"tap:{tk.label}"[:80] if token else "kiosk"
        if payload["action"] == "in":
            slot = L.clock_in(db, st, sh, who=who)
            msg = f"{st.name} clocked in on {sh.name} at {fmt_time(slot.start)}"
        else:
            slot = L.clock_out(db, st, sh, who=who)
            msg = f"{st.name} clocked out of {sh.name} at {fmt_time(slot.end)}"
            if payload["action"] == "late_out":
                msg += ". The automatic end was replaced. Admin will check it."
        if token:
            db.add(M.Tap(at=now_utc(), token=token, streamer=st.name, shop=sh.name,
                         result=f"success: {payload['action']}", ip=client_ip(request)))
        db.commit()
        return _kiosk_msg(request, db, msg, ok=True, back="/clock/done" if token else "/clock")
    except L.ClockError as e:
        db.rollback()
        if token:
            tap_record(db, request, token, f"refused: {e}", st, sh)
        else:
            return clock_form(request, db, str(e), st.id if st else "", sh.id if sh else "")
        return _kiosk_msg(request, db, str(e), back="/clock/done" if token else "/clock")
    except IntegrityError:
        db.rollback()
        msg = "This slot changed at the same time. Tap again or open the clock to check it."
        if token:
            tap_record(db, request, token, f"refused: {msg}", st, sh)
        logger.warning("Concurrent clock update refused", exc_info=True)
        if not token:
            return clock_form(request, db, msg, st.id if st else "", sh.id if sh else "")
        return _kiosk_msg(request, db, msg, back="/clock/done" if token else "/clock")


def _kiosk_msg(request, db, msg, ok=False, back="/clock"):
    return render(request, "message.html", msg=msg, ok=ok, back=back, msg_seconds=setting_int(db, "message_seconds"))


# ---- tap boxes -------------------------------------------------------------------
@app.post("/t/{token}/person", response_class=HTMLResponse)
def tap_person(request: Request, token: str, streamer_id: str = Form(""), db: Session = Depends(get_db)):
    tk = db.scalars(select(M.Token).where(M.Token.token == token, M.Token.active.is_(True))).first()
    if not tk:
        raise HTTPException(404, "This tag is turned off or was not found.")
    if not rate_allowed(request, token):
        return tap_person_page(request, db, token, "Too many requests. Wait a minute and try again.")
    if remembered_person(request, db) or tag_destination(db, tk) != "assigned":
        return RedirectResponse(f"/t/{token}", status_code=303)
    try:
        person = db.get(M.Streamer, int(streamer_id))
    except ValueError:
        person = None
    if not person or not person.active:
        return tap_person_page(request, db, token, "Pick your name from the list.")
    response = RedirectResponse(f"/t/{token}?remembered=1", status_code=303)
    response.set_cookie(PERSON_COOKIE, person_signer.dumps({"id": person.id, "session": secrets.token_urlsafe(24)}),
                        max_age=PERSON_MAX_AGE, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")
    tap_record(db, request, token, "remembered person", person)
    return response


@app.post("/t/{token}/signout")
def tap_signout(request: Request, token: str, db: Session = Depends(get_db)):
    if db.scalars(select(M.Token).where(M.Token.token == token)).first() is None:
        raise HTTPException(404, "That tag was not found.")
    tap_record(db, request, token, "signed out")
    response = RedirectResponse(f"/t/{token}", status_code=303)
    response.delete_cookie(PERSON_COOKIE, path="/", httponly=True, samesite="lax",
                           secure=request.url.scheme == "https")
    return response


@app.get("/t/{token}", response_class=HTMLResponse)
def tap(request: Request, token: str, db: Session = Depends(get_db)):
    ip = client_ip(request)
    tk = db.scalars(select(M.Token).where(M.Token.token == token, M.Token.active.is_(True))).first()
    if not rate_allowed(request, token):
        tap_record(db, request, token, "refused: too many taps")
        return _kiosk_msg(request, db, "Too many taps. Wait a minute and try again.", back="/clock/done")
    if not tk:
        tap_record(db, request, token, "unknown or inactive tag")
        return _kiosk_msg(request, db, "This tag isn't set up. Use the clock screen on the computer.", back="/clock")
    destination = tag_destination(db, tk)
    if destination != "assigned":
        tap_record(db, request, token, f"redirect: {destination}")
        return RedirectResponse(destination, status_code=302)
    st = remembered_person(request, db)
    if not st:
        tap_record(db, request, token, "pick person")
        error = "This phone did not keep your name. Allow cookies for this site and try again." if request.query_params.get("remembered") else ""
        return tap_person_page(request, db, token, error)
    shop = L.scheduled_shop_now(db, st)
    if shop is None:
        tap_record(db, request, token, "pick shop", st)
        return render(request, "tap_shop.html", token=token, streamer=st, shops=active_shops(db))
    return _tap_question(request, db, tk, st, shop, ip)


@app.post("/t/{token}/shop", response_class=HTMLResponse)
def tap_shop(request: Request, token: str, shop_id: int = Form(...), db: Session = Depends(get_db)):
    tk = db.scalars(select(M.Token).where(M.Token.token == token, M.Token.active.is_(True))).first()
    if not rate_allowed(request, token):
        tap_record(db, request, token, "refused: too many requests")
        return _kiosk_msg(request, db, "Too many requests. Wait a minute and try again.", back="/clock/done")
    if not tk:
        tap_record(db, request, token, "refused: tag turned off")
        return _kiosk_msg(request, db, "This tag isn't set up.", back="/clock")
    if tag_destination(db, tk) != "assigned":
        return RedirectResponse(f"/t/{token}", status_code=303)
    st = remembered_person(request, db)
    if not st:
        return RedirectResponse(f"/t/{token}", status_code=303)
    sh = db.get(M.Shop, shop_id)
    if sh is None or not sh.active:
        tap_record(db, request, token, "refused: inactive or unknown shop", st, sh)
        return render(request, "tap_shop.html", token=token, streamer=st, shops=active_shops(db),
                      error="Pick a shop from the list.")
    return _tap_question(request, db, tk, st, sh, client_ip(request))


@app.get("/t/{token}/choose", response_class=HTMLResponse)
def tap_choose(request: Request, token: str, db: Session = Depends(get_db)):
    tk = db.scalars(select(M.Token).where(M.Token.token == token, M.Token.active.is_(True))).first()
    if not tk:
        return RedirectResponse("/clock", status_code=303)
    if tag_destination(db, tk) != "assigned":
        return RedirectResponse(f"/t/{token}", status_code=303)
    st = remembered_person(request, db)
    if not st:
        return RedirectResponse(f"/t/{token}", status_code=303)
    return render(request, "tap_shop.html", token=token, streamer=st, shops=active_shops(db))


def _tap_question(request, db, tk, st, shop, ip):
    action = "out" if L.open_slot(db, st, shop) or L.late_slot(db, st, shop) else "in"
    try:
        L.check_clock(db, st, shop, action)
        tap_record(db, request, tk.token, f"ask {action}", st, shop)
        return question(request, db, st, shop, action, tk.token)
    except L.ClockError as e:
        db.rollback()
        tap_record(db, request, tk.token, f"refused: {e}", st, shop)
        return _kiosk_msg(request, db, str(e), back="/clock/done")


@app.post("/t/{token}/new", response_class=HTMLResponse)
def tap_new(request: Request, token: str, shop_id: int = Form(...), db: Session = Depends(get_db)):
    tk = db.scalars(select(M.Token).where(M.Token.token == token, M.Token.active.is_(True))).first()
    if not rate_allowed(request, token) or not tk:
        tap_record(db, request, token, "refused: new stream request")
        return _kiosk_msg(request, db, "Open the clock screen to start a new stream.", back="/clock/done")
    if tag_destination(db, tk) != "assigned":
        return RedirectResponse(f"/t/{token}", status_code=303)
    st = remembered_person(request, db)
    if not st:
        return RedirectResponse(f"/t/{token}", status_code=303)
    sh = db.get(M.Shop, shop_id)
    if not sh:
        raise HTTPException(400, "Pick a shop from the list.")
    try:
        tap_record(db, request, token, "ask new clock in", st, sh)
        return question(request, db, st, sh, "in", token)
    except L.ClockError as e:
        tap_record(db, request, token, f"refused: {e}", st, sh)
        return _kiosk_msg(request, db, str(e), back="/clock/done")


@app.post("/t/{token}/confirm", response_class=HTMLResponse)
def tap_confirm(request: Request, token: str, ticket: str = Form(""), db: Session = Depends(get_db)):
    return _do_clock(request, db, ticket, token)


# ---- day view --------------------------------------------------------------------
@app.get("/day", response_class=HTMLResponse)
def day_view(request: Request, date: str | None = None, db: Session = Depends(get_db)):
    d = parse_day(date)
    slots = L.slots_for_day(db, d)
    hints = {s.id: L.mismatch_hint(db, s) for s in slots}
    return render(request, "day.html", day=d, slots=slots, hints=hints, noshows=L.no_shows(db, d),
                  prev=(d - timedelta(days=1)).isoformat(), next=(d + timedelta(days=1)).isoformat(),
                  shops=active_shops(db))


@app.get("/day.csv")
def day_csv(date: str | None = None, db: Session = Depends(get_db)):
    d = parse_day(date)
    return Response(S.to_csv(S.today_rows(db, d)), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=slots-{d}.csv"})


@app.get("/log.csv")
def log_csv(request: Request, start: str | None = None, end: str | None = None, db: Session = Depends(get_db)):
    require_admin(request)
    rows = S.log_rows(db, parse_day(start) if start else None, parse_day(end) if end else None)
    return Response(S.to_csv(rows), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=stream-log.csv"})


@app.get("/admin/slots", response_class=HTMLResponse)
def slot_log(request: Request, start: str | None = None, end: str | None = None, page: int = 1,
             db: Session = Depends(get_db)):
    require_admin(request)
    first = parse_day(start) if start else to_local(now_utc()).date() - timedelta(days=30)
    last = parse_day(end)
    if first > last:
        raise HTTPException(400, "The first date must be on or before the last date.")
    if page < 1:
        raise HTTPException(400, "Choose a page starting at 1.")
    rows = list(db.scalars(select(M.Slot).where(M.Slot.day >= first, M.Slot.day <= last)
                           .order_by(M.Slot.start, M.Slot.id).offset((page - 1) * 100).limit(101)))
    return render(request, "admin/slots.html", slots=rows[:100], start=first, end=last,
                  page=page, more=len(rows) > 100)


@app.post("/day/use-schedule")
def day_use_schedule(request: Request, day: str = Form(...), streamer_id: int = Form(...), shop_id: int = Form(...),
                     start_time: str = Form(...), back: str = Form("/day"), db: Session = Depends(get_db)):
    require_admin(request)
    L.use_schedule(db, parse_day(day), streamer_id, shop_id, start_time)
    db.commit()
    target = f"/sales?date={parse_day(day)}&shop_id={shop_id}" if back == "/sales" else f"/day?date={parse_day(day)}"
    return RedirectResponse(target, status_code=303)


@app.post("/day/dismiss")
def day_dismiss(request: Request, day: str = Form(...), streamer_id: int = Form(...), shop_id: int = Form(...),
                start_time: str = Form(...), db: Session = Depends(get_db)):
    require_admin(request)
    L.dismiss(db, parse_day(day), streamer_id, shop_id, start_time)
    db.commit()
    return RedirectResponse(f"/day?date={day}", status_code=303)


# ---- attribution -----------------------------------------------------------------
@app.get("/sales", response_class=HTMLResponse)
def sales(request: Request, date: str | None = None, shop_id: int | None = None,
          msg: str = "", db: Session = Depends(get_db)):
    require_admin(request)
    d = parse_day(date)
    view_shops, selected = sales_shops(db, shop_id)
    res = A.attribute(db, d, selected)
    uploads = list(db.scalars(select(M.Upload).where(
        M.Upload.day == d, M.Upload.shop_id == selected.id if selected else True).order_by(M.Upload.uploaded_at)))
    gaps = [g for g in A.gaps_with_schedule(db, d) if selected is None or g.shop.id == selected.id]
    return render(request, "sales.html", day=d, res=res, uploads=uploads, shops=active_shops(db), msg=msg,
                  selected=selected, view_shops=view_shops,
                  gaps=gaps if any(l.unassigned for l in res.lines) else [],
                  prev=(d - timedelta(days=1)).isoformat(), next=(d + timedelta(days=1)).isoformat())


@app.post("/sales/upload")
async def sales_upload(request: Request, day: str = Form(...), shop_id: int = Form(...), file: UploadFile = File(...),
                       db: Session = Depends(get_db)):
    require_admin(request)
    d = parse_day(day)
    shop = db.scalars(select(M.Shop).where(M.Shop.id == shop_id).with_for_update()).first()
    if not shop or not shop.active:
        raise HTTPException(400, "Pick an active shop.")
    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "This file is too large. Export just this LIVE session (up to 25 MB).")
    filename = Path(file.filename or "export.csv").name
    if len(filename) > 200:
        raise HTTPException(400, "Shorten this file's name to 200 characters or fewer and try again.")
    try:
        up = A.import_export(db, shop, d, filename, data)
        db.flush()
        res = A.attribute(db, d, shop)
        db.add(M.AttributionRun(day=d, shop_id=shop.id, upload_id=up.id, at=now_utc(), who="Admin",
                                result=A.result_csv(res)))
        db.commit()
        msg = f"Read {up.rows} orders for {shop.name}; {up.added} new, {up.rows - up.added} already there."
    except A.ImportError_ as e:
        db.rollback()
        msg = f"Couldn't read that file: {e}"
    return redirect_message(f"/sales?date={d}&shop_id={shop_id}", msg)


@app.get("/sales.csv")
def sales_csv(request: Request, date: str | None = None, shop_id: int | None = None, db: Session = Depends(get_db)):
    require_admin(request)
    d = parse_day(date)
    _, selected = sales_shops(db, shop_id)
    return Response(A.result_csv(A.attribute(db, d, selected)), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=sales-by-slot-{selected.id if selected else 'none'}-{d}.csv"})


@app.post("/sheets/sync")
def sheets_sync(request: Request, day: str = Form(""), back: str = Form("/day"), db: Session = Depends(get_db)):
    require_admin(request)
    try:
        msg = S.sync(db, parse_day(day) if day else None)
    except S.SheetsError as exc:
        db.rollback()
        logger.exception("Manual Sheets sync failed")
        raise HTTPException(502, f"Google Sheets could not be updated. Your slots are safe. {exc}")
    target = back if urlsplit(back).path in ("/day", "/sales", "/admin") and not urlsplit(back).netloc else "/admin"
    return redirect_message(target, msg)


# ---- admin -------------------------------------------------------------------------
@app.get("/admin/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/admin"):
    return render(request, "admin/login.html", next=next, error="")


@app.post("/admin/login")
def login(request: Request, password: str = Form(...), next: str = Form("/admin")):
    allowed = rate_ok(f"login:{client_ip(request)}", limit=10)
    if not allowed or not ADMIN_PASSWORD or not hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        return render(request, "admin/login.html", next=next, error="That password didn't work.")
    r = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/admin", status_code=303)
    r.set_cookie("admin", signer.dumps(hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()), httponly=True,
                 secure=os.environ.get("PUBLIC_URL", "").startswith("https://"),
                 samesite="lax", max_age=60 * 60 * 24 * 30)
    return r


@app.get("/admin/logout")
def logout():
    r = RedirectResponse("/clock", status_code=303)
    r.delete_cookie("admin")
    return r


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, msg: str = "", db: Session = Depends(get_db)):
    require_admin(request)
    flagged = list(db.scalars(select(M.Slot).where(M.Slot.status == "auto_closed", M.Slot.note != "reviewed").order_by(M.Slot.start)))
    recent = list(db.scalars(select(M.AuditLog).order_by(desc(M.AuditLog.id)).limit(15)))
    return render(request, "admin/home.html", open_slots=L.open_slots(db), flagged=flagged, recent=recent, msg=msg,
                  sheets_ok=S.configured(), last_sync=get_setting(db, "last_sheets_sync"))


@app.post("/admin/slot/{slot_id}/reviewed")
def slot_reviewed(request: Request, slot_id: int, db: Session = Depends(get_db)):
    require_admin(request)
    s = db.get(M.Slot, slot_id)
    if not s:
        raise HTTPException(404, "That slot was not found.")
    L.lock_streamer(db, s.streamer_id)
    db.refresh(s)
    before = L.slot_dict(s)
    s.note = "reviewed"
    audit(db, "Admin", "reviewed", "slot", s.id, before=before, after=L.slot_dict(s))
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/slots/new", response_class=HTMLResponse)
def new_slot(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return render(request, "admin/slot.html", slot=M.Slot(status="estimated", note="added by Admin"),
                  streamers=active_streamers(db), shops=active_shops(db),
                  start=to_local(now_utc()).replace(tzinfo=None).isoformat(timespec="seconds"), end="")


@app.post("/admin/slots/new")
def create_slot(request: Request, streamer_id: int = Form(...), shop_id: int = Form(...),
                start: str = Form(...), end: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request)
    st, sh = person_shop(db, streamer_id, shop_id)
    L.lock_streamer(db, st.id)
    L.lock_shop(db, sh.id)
    if not st.active or not sh.active:
        raise HTTPException(400, "Pick a name and shop that are on the list.")
    try:
        first, last = parse_local_input(start), parse_local_input(end) if end else None
    except ValueError as exc:
        raise HTTPException(400, f"Check the start and end times. {exc}")
    if last is not None and last < first:
        raise HTTPException(400, "The end cannot be before the start. For an overnight slot, use the next date.")
    if last is None:
        L.check_clock(db, st, sh, "in")
    s = M.Slot(day=local_day(first), streamer_id=st.id, shop_id=sh.id, start=first, end=last,
               status="estimated" if last else "open", note="added by Admin")
    db.add(s)
    db.flush()
    audit(db, "Admin", "add", "slot", s.id, after=L.slot_dict(s))
    db.commit()
    return RedirectResponse(f"/day?date={s.day}", status_code=303)


@app.get("/admin/slot/{slot_id}", response_class=HTMLResponse)
def slot_edit(request: Request, slot_id: int, db: Session = Depends(get_db)):
    require_admin(request)
    s = db.get(M.Slot, slot_id)
    if not s:
        raise HTTPException(404)
    return render(request, "admin/slot.html", slot=s,
                  streamers=list(db.scalars(select(M.Streamer).order_by(M.Streamer.sort, M.Streamer.name))),
                  shops=list(db.scalars(select(M.Shop).order_by(M.Shop.sort, M.Shop.name))),
                  start=to_local(s.start).replace(tzinfo=None).isoformat(timespec="seconds"),
                  end=to_local(s.end).replace(tzinfo=None).isoformat(timespec="seconds") if s.end else "")


@app.post("/admin/slot/{slot_id}")
def slot_save(request: Request, slot_id: int, streamer_id: int = Form(...), shop_id: int = Form(...), start: str = Form(...),
              end: str = Form(""), delete: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request)
    s = db.get(M.Slot, slot_id)
    if not s:
        raise HTTPException(404, "That slot was not found.")
    st, sh = person_shop(db, streamer_id, shop_id)
    for sid in sorted({s.streamer_id, streamer_id}):
        L.lock_streamer(db, sid)
    db.refresh(s)
    before = L.slot_dict(s)
    day = s.day
    if delete:
        audit(db, "Admin", "delete", "slot", s.id, before=before)
        db.delete(s)
    else:
        try:
            new_start = parse_local_input(start)
            new_end = parse_local_input(end) if end else None
        except ValueError as exc:
            raise HTTPException(400, f"Check the start and end times. {exc}")
        if new_end is not None and new_end < new_start:
            raise HTTPException(400, "The end cannot be before the start. For an overnight slot, use the next date.")
        if new_end is None:
            L.lock_shop(db, sh.id)
            if not st.active or not sh.active:
                raise HTTPException(400, "Show this name and shop on the list before reopening the slot.")
            q = select(M.Slot).where(M.Slot.id != s.id, M.Slot.streamer_id == streamer_id, M.Slot.end.is_(None))
            if get_setting(db, "allow_both_shops") == "1":
                q = q.where(M.Slot.shop_id == shop_id)
            if db.scalars(q).first():
                raise HTTPException(409, "That streamer already has an open slot. Close it before reopening this one.")
        s.streamer_id, s.shop_id = st.id, sh.id
        s.start, s.end = new_start, new_end
        s.day = local_day(s.start)
        if s.end and s.status == "open":
            s.status = "closed"
        if not s.end:
            s.status = "open"
        elif s.status == "auto_closed":
            s.status, s.note = "closed", "reviewed"
        audit(db, "Admin", "edit", "slot", s.id, before=before, after=L.slot_dict(s))
        day = s.day
    db.commit()
    return RedirectResponse(f"/day?date={day}", status_code=303)


@app.get("/admin/people", response_class=HTMLResponse)
def people(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    streamers = list(db.scalars(select(M.Streamer).order_by(M.Streamer.sort, M.Streamer.name)))
    shops = list(db.scalars(select(M.Shop).order_by(M.Shop.sort, M.Shop.name)))
    return render(request, "admin/people.html", streamers=streamers, shops=shops)


@app.post("/admin/people/streamer")
def streamer_save(request: Request, name: str = Form(""), toggle_id: int | None = Form(None),
                  edit_id: int | None = Form(None), db: Session = Depends(get_db)):
    require_admin(request)
    if edit_id:
        return rename_person(db, M.Streamer, edit_id, name)
    if toggle_id:
        s = db.get(M.Streamer, toggle_id)
        if not s:
            raise HTTPException(404, "That streamer was not found.")
        L.lock_streamer(db, s.id)
        before = {"name": s.name, "active": s.active}
        if s.active and db.scalars(select(M.Slot).where(M.Slot.streamer_id == s.id, M.Slot.end.is_(None))).first():
            raise HTTPException(400, "Close this streamer's open slots before hiding them.")
        s.active = not s.active
        audit(db, "Admin", "toggle", "streamer", s.id, before=before, after={"name": s.name, "active": s.active})
    elif name.strip():
        if len(name.strip()) > 80:
            raise HTTPException(400, "Use a name with 80 letters or fewer.")
        s = M.Streamer(name=name.strip(), sort=99)
        db.add(s); db.flush()
        audit(db, "Admin", "add", "streamer", s.id, after={"name": s.name})
    db.commit()
    return RedirectResponse("/admin/people", status_code=303)


@app.post("/admin/people/shop")
def shop_save(request: Request, name: str = Form(""), toggle_id: int | None = Form(None),
              edit_id: int | None = Form(None), db: Session = Depends(get_db)):
    require_admin(request)
    if edit_id:
        return rename_person(db, M.Shop, edit_id, name)
    if toggle_id:
        s = db.get(M.Shop, toggle_id)
        if not s:
            raise HTTPException(404, "That shop was not found.")
        L.lock_shop(db, s.id)
        before = {"name": s.name, "active": s.active}
        if s.active and db.scalars(select(M.Slot).where(M.Slot.shop_id == s.id, M.Slot.end.is_(None))).first():
            raise HTTPException(400, "Close this shop's open slots before hiding it.")
        s.active = not s.active
        audit(db, "Admin", "toggle", "shop", s.id, before=before, after={"name": s.name, "active": s.active})
    elif name.strip():
        if len(name.strip()) > 80:
            raise HTTPException(400, "Use a shop name with 80 letters or fewer.")
        s = M.Shop(name=name.strip().upper(), sort=99)
        db.add(s); db.flush()
        audit(db, "Admin", "add", "shop", s.id, after={"name": s.name})
    db.commit()
    return RedirectResponse("/admin/people", status_code=303)


def rename_person(db, model, item_id, name):
    item = db.get(model, item_id)
    if item is None:
        raise HTTPException(404, "That name was not found.")
    name = name.strip().upper() if model is M.Shop else name.strip()
    if not name or len(name) > 80:
        raise HTTPException(400, "Enter a name with 1 to 80 letters.")
    before = {"name": item.name, "active": item.active}
    item.name = name
    audit(db, "Admin", "edit", "shop" if model is M.Shop else "streamer", item.id,
          before=before, after={"name": item.name, "active": item.active})
    db.commit()
    return RedirectResponse("/admin/people", status_code=303)


@app.get("/admin/tags", response_class=HTMLResponse)
def tags(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    base = os.environ.get("PUBLIC_URL", str(request.base_url).rstrip("/"))
    tokens = list(db.scalars(select(M.Token).order_by(M.Token.active.desc(), M.Token.id)))
    return render(request, "admin/tags.html", tokens=tokens, base=base, streamers=active_streamers(db), shops=active_shops(db))


def tag_routing_page(request, db, error="", draft=None):
    tokens = list(db.scalars(select(M.Token).order_by(M.Token.active.desc(), M.Token.id)))
    routes = {route.token_id: route.destination for route in db.scalars(select(M.TagRoute))}
    routes[0] = get_setting(db, "tag_default_destination") or "assigned"
    response = render(request, "admin/tag_routing.html", tokens=tokens, routes=routes,
                      destinations=TAG_DESTINATIONS, error=error, draft=draft,
                      base=os.environ.get("PUBLIC_URL", str(request.base_url).rstrip("/")))
    if error:
        response.status_code = 400
    return response


@app.get("/admin/tag-routing", response_class=HTMLResponse)
def tag_routing(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return tag_routing_page(request, db)


@app.post("/admin/tag-routing", response_class=HTMLResponse)
def tag_routing_save(request: Request, token_id: int = Form(...), destination: str = Form(...),
                     custom_url: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request)
    if token_id != 0 and db.get(M.Token, token_id) is None:
        raise HTTPException(404, "That tag was not found.")
    try:
        value = validate_tag_destination(destination, custom_url)
        if token_id == 0 and value == "inherit":
            raise ValueError("Choose a shared destination, not the shared default itself.")
    except ValueError as exc:
        return tag_routing_page(request, db, str(exc),
                                {"token_id": token_id, "destination": destination, "custom_url": custom_url})
    if token_id == 0:
        before = get_setting(db, "tag_default_destination") or "assigned"
        set_setting(db, "tag_default_destination", value)
    else:
        route = db.get(M.TagRoute, token_id)
        before = route.destination if route else "inherit"
        if value == "inherit":
            if route:
                db.delete(route)
        elif route:
            route.destination = value
        else:
            db.add(M.TagRoute(token_id=token_id, destination=value))
    audit(db, "Admin", "edit", "tag_route", token_id,
          before={"destination": before}, after={"destination": value})
    db.commit()
    return RedirectResponse("/admin/tag-routing?saved=1", status_code=303)


@app.post("/admin/tags")
def tag_save(request: Request, label: str = Form(""),
             revoke_id: int | None = Form(None), edit_id: int | None = Form(None), db: Session = Depends(get_db)):
    require_admin(request)
    if len(label.strip()) > 120:
        raise HTTPException(400, "Use a label with 120 letters or fewer.")
    if revoke_id:
        t = db.get(M.Token, revoke_id)
        if t is None:
            raise HTTPException(404, "That tag was not found.")
        before = {"label": t.label, "active": t.active}
        t.active = not t.active
        audit(db, "Admin", "toggle", "token", t.id, before=before, after={"label": t.label, "active": t.active})
    elif edit_id:
        t = db.get(M.Token, edit_id)
        if t is None:
            raise HTTPException(404, "That tag was not found.")
        if not label.strip():
            raise HTTPException(400, "Give the shared box a label.")
        before = {"label": t.label}
        t.label = label.strip()
        audit(db, "Admin", "edit", "token", t.id, before=before, after={"label": t.label})
    else:
        if not label.strip():
            raise HTTPException(400, "Give the shared box a label.")
        t = M.Token(token=secrets.token_urlsafe(24), label=label.strip())
        db.add(t); db.flush()
        audit(db, "Admin", "add", "token", t.id, after={"label": t.label})
    db.commit()
    return RedirectResponse("/admin/tags", status_code=303)


@app.get("/admin/settings", response_class=HTMLResponse)
def settings(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    vals = {k: get_setting(db, k) for k in DEFAULT_SETTINGS}
    return render(request, "admin/settings.html", v=vals, sheets_ok=S.configured())


@app.post("/admin/settings")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    form = await request.form()
    before = {k: get_setting(db, k) for k in DEFAULT_SETTINGS}
    bounds = {"auto_close_hours": (1, 24), "auto_close_grace_min": (0, 240), "noshow_after_min": (0, 240),
              "smart_default_before_min": (0, 240), "message_seconds": (5, 5)}
    for k, (low, high) in bounds.items():
        if k in form and str(form[k]).strip():
            try:
                value = int(str(form[k]))
            except ValueError:
                raise HTTPException(400, "Use whole numbers for the timers.")
            if not low <= value <= high:
                raise HTTPException(400, f"Use a value from {low} to {high}. Messages always stay for 5 seconds.")
            set_setting(db, k, str(value))
    set_setting(db, "allow_both_shops", "1" if form.get("allow_both_shops") else "0")
    audit(db, "Admin", "edit", "settings", None, before=before, after={k: get_setting(db, k) for k in DEFAULT_SETTINGS})
    db.commit()
    return RedirectResponse("/admin/settings", status_code=303)


def validate_schedule(db, streamer_id, shop_id, start, end):
    st, sh = person_shop(db, streamer_id, shop_id)
    if not st.active or not sh.active:
        raise HTTPException(400, "Choose a name and shop that are on the list.")
    try:
        for value in (start, end):
            datetime.strptime(value, "%H:%M")
    except ValueError:
        raise HTTPException(400, "Choose a start and end time.")
    if start == end:
        raise HTTPException(400, "Choose different start and end times.")


@app.get("/admin/schedule", response_class=HTMLResponse)
def schedule(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    rules = list(db.scalars(select(M.ScheduleRule).order_by(M.ScheduleRule.weekday, M.ScheduleRule.start_time)))
    overrides = list(db.scalars(select(M.ScheduleOverride).where(M.ScheduleOverride.day >= to_local(now_utc()).date() - timedelta(days=7))
                                .order_by(M.ScheduleOverride.day)))
    return render(request, "admin/schedule.html", rules=rules, overrides=overrides, streamers=active_streamers(db),
                  shops=active_shops(db), days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])


@app.post("/admin/schedule/rule")
def rule_save(request: Request, streamer_id: int = Form(0), shop_id: int = Form(0), weekdays: list[int] = Form([]),
              start_time: str = Form(""), end_time: str = Form(""), delete_id: int | None = Form(None), db: Session = Depends(get_db)):
    require_admin(request)
    if delete_id:
        r = db.get(M.ScheduleRule, delete_id)
        if not r:
            raise HTTPException(404, "That schedule was not found.")
        audit(db, "Admin", "delete", "schedule_rule", r.id, before={"streamer": r.streamer.name, "shop": r.shop.name, "weekday": r.weekday, "start": r.start_time, "end": r.end_time})
        db.delete(r)
    elif streamer_id and shop_id and start_time and end_time:
        validate_schedule(db, streamer_id, shop_id, start_time, end_time)
        if not weekdays or any(wd not in range(7) for wd in weekdays):
            raise HTTPException(400, "Pick at least one valid day.")
        for wd in sorted(set(weekdays)):
            r = M.ScheduleRule(streamer_id=streamer_id, shop_id=shop_id, weekday=wd, start_time=start_time[:5], end_time=end_time[:5])
            db.add(r); db.flush()
            audit(db, "Admin", "add", "schedule_rule", r.id, after={"streamer_id": streamer_id, "shop_id": shop_id, "weekday": wd, "start": start_time, "end": end_time})
    db.commit()
    return RedirectResponse("/admin/schedule", status_code=303)


@app.post("/admin/schedule/override")
def override_save(request: Request, day: str = Form(""), streamer_id: int = Form(0), shop_id: int = Form(0), start_time: str = Form(""),
                  end_time: str = Form(""), kind: str = Form("add"), delete_id: int | None = Form(None), db: Session = Depends(get_db)):
    require_admin(request)
    if delete_id:
        o = db.get(M.ScheduleOverride, delete_id)
        if not o:
            raise HTTPException(404, "That schedule change was not found.")
        audit(db, "Admin", "delete", "schedule_override", o.id,
              before={"day": o.day, "streamer_id": o.streamer_id, "shop_id": o.shop_id,
                      "start": o.start_time, "end": o.end_time, "kind": o.kind})
        db.delete(o)
    elif day and streamer_id and shop_id and start_time:
        if kind not in ("add", "cancel"):
            raise HTTPException(400, "Choose Add or Cancel.")
        if kind == "add":
            validate_schedule(db, streamer_id, shop_id, start_time, end_time)
        else:
            person_shop(db, streamer_id, shop_id)
            try:
                datetime.strptime(start_time, "%H:%M")
            except ValueError:
                raise HTTPException(400, "Choose the usual slot's start time.")
        o = M.ScheduleOverride(day=parse_day(day), streamer_id=streamer_id, shop_id=shop_id, start_time=start_time,
                               end_time=end_time or start_time, kind=kind)
        db.add(o)
        db.flush()
        audit(db, "Admin", "add", "schedule_override", o.id,
              after={"day": o.day, "streamer_id": streamer_id, "shop_id": shop_id,
                     "start": o.start_time, "end": o.end_time, "kind": kind})
    db.commit()
    return RedirectResponse("/admin/schedule", status_code=303)


def history_details(db, raw):
    if not raw:
        return []
    values = json.loads(raw)
    labels = {"id": "Slot", "day": "Date", "streamer_id": "Streamer", "shop_id": "Shop", "start": "Start",
              "end": "End", "status": "Status", "note": "Note", "name": "Name", "active": "On the list",
              "label": "Tag label", "kind": "Change", "file": "File", "rows": "Orders read", "added": "New orders",
              "weekday": "Day of week", "auto_close_hours": "Close unscheduled slots after (hours)",
              "auto_close_grace_min": "Scheduled end grace (minutes)", "message_seconds": "Message seconds",
              "allow_both_shops": "Allow both shops", "noshow_after_min": "Missing clock-in wait (minutes)",
              "smart_default_before_min": "Schedule shop selection lead (minutes)", "last_sheets_sync": "Last Sheets send"}
    out = []
    for key, value in values.items():
        if key in ("streamer_id", "shop_id") and value:
            row = db.get(M.Streamer if key == "streamer_id" else M.Shop, value)
            value = row.name if row else "Removed"
        elif key in ("start", "end") and value:
            try:
                dt = datetime.fromisoformat(value)
                value = to_local(dt).strftime("%-m/%-d/%Y %-I:%M %p")
            except ValueError:
                logger.debug("History has a schedule time rather than a timestamp")
        elif key == "status":
            value = {"open": "Still open", "closed": "Closed", "auto_closed": "Closed automatically",
                     "estimated": "From schedule"}.get(value, value)
        if isinstance(value, bool):
            value = "Yes" if value else "No"
        out.append((labels.get(key, key.replace("_", " ").capitalize()), value if value is not None else "Still open"))
    return out


@app.get("/admin/log", response_class=HTMLResponse)
def audit_view(request: Request, page: int = 1, db: Session = Depends(get_db)):
    require_admin(request)
    if page < 1:
        raise HTTPException(400, "Choose a page starting at 1.")
    rows = list(db.scalars(select(M.AuditLog).order_by(desc(M.AuditLog.id)).offset((page - 1) * 100).limit(101)))
    taps = list(db.scalars(select(M.Tap).order_by(desc(M.Tap.id)).limit(100)))
    details = {r.id: (history_details(db, r.before), history_details(db, r.after)) for r in rows[:100]}
    return render(request, "admin/log.html", rows=rows[:100], taps=taps, details=details, page=page, more=len(rows) > 100)
