import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import sessionmaker

from app import models as M, logic as L, db as D
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import main as web
from test_web import ticket


@pytest.fixture
def postgres_sessions():
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("Set POSTGRES_TEST_URL to a disposable Postgres database for concurrency tests.")
    engine = create_engine(url)
    schema = "clock_test_" + uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        M.Base.metadata.create_all(isolated)
        sessions = sessionmaker(bind=isolated, expire_on_commit=False)
        with sessions() as db:
            db.add_all([M.Streamer(id=1, name="Streamer A"), M.Shop(id=1, name="Shop A"),
                        M.Shop(id=2, name="Shop B")])
            db.commit()
        yield sessions
    finally:
        isolated.dispose()
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.parametrize("both_shops", [True, False])
def test_concurrent_clock_ins_preserve_rules(postgres_sessions, both_shops):
    with postgres_sessions() as db:
        db.add(M.Setting(key="allow_both_shops", value="1" if both_shops else "0"))
        db.commit()
    barrier = Barrier(2)

    def clock(shop_id):
        with postgres_sessions() as db:
            st, sh = db.get(M.Streamer, 1), db.get(M.Shop, shop_id)
            barrier.wait(timeout=10)
            try:
                L.clock_in(db, st, sh)
                db.commit()
                return "created"
            except L.ClockError:
                db.rollback()
                return "refused"

    # Same shop duplicates, and cross-shop races when that setting is blocked.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(clock, 1), pool.submit(clock, 1 if both_shops else 2)]
        results = [f.result(timeout=15) for f in futures]
    assert sorted(results) == ["created", "refused"]
    with postgres_sessions() as db:
        assert len(list(db.scalars(select(M.Slot)))) == 1


def test_concurrent_clock_out_only_changes_slot_once(postgres_sessions):
    with postgres_sessions() as db:
        L.clock_in(db, db.get(M.Streamer, 1), db.get(M.Shop, 1))
        db.commit()
    barrier = Barrier(2)

    def clock():
        with postgres_sessions() as db:
            st, sh = db.get(M.Streamer, 1), db.get(M.Shop, 1)
            barrier.wait(timeout=10)
            try:
                L.clock_out(db, st, sh)
                db.commit()
                return "closed"
            except L.ClockError:
                db.rollback()
                return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(clock), pool.submit(clock)]
        assert sorted(f.result(timeout=15) for f in futures) == ["closed", "refused"]
    with postgres_sessions() as db:
        changes = list(db.scalars(select(M.AuditLog).where(M.AuditLog.action == "clock_out")))
        assert len(changes) == 1


def test_shared_box_migration_preserves_postgres_tag_routes(postgres_sessions, monkeypatch):
    engine = postgres_sessions.kw["bind"]
    with postgres_sessions() as db:
        db.add(M.Token(id=8, token="old-tag", label="Old box", streamer_id=1, shop_id=1))
        db.flush()
        db.add(M.TagRoute(token_id=8, destination="/day"))
        db.commit()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE tokens ALTER COLUMN streamer_id SET NOT NULL"))
    monkeypatch.setattr(D, "engine", engine)
    monkeypatch.setattr(D, "SessionLocal", postgres_sessions)
    D.init_db()
    D.init_db()
    with postgres_sessions() as db:
        assert db.get(M.Token, 8).token == "old-tag"
        assert db.get(M.TagRoute, 8).destination == "/day"
        db.add(M.Token(token="new-shared-tag", label="New shared box"))
        db.commit()
        assert db.scalars(select(M.Token).where(M.Token.token == "new-shared-tag")).one().streamer_id is None


def test_concurrent_confirmations_and_shared_rate_limit(postgres_sessions):
    def session_dependency():
        with postgres_sessions() as db:
            yield db

    web.app.dependency_overrides[web.get_db] = session_dependency
    try:
        with patch.object(web, "SessionLocal", postgres_sessions):
            c = TestClient(web.app)
            questions = [ticket(c.post("/clock/ask", data={"streamer_id": 1, "shop_id": 1, "action": "in"}))
                         for _ in range(2)]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(c.post, "/clock/confirm", data={"ticket": q}) for q in questions]
                responses = [f.result(timeout=15).text for f in futures]
            assert sum("Streamer A clocked in" in text for text in responses) == 1
            assert sum("already clocked in" in text for text in responses) == 1
            with postgres_sessions() as db:
                assert len(list(db.scalars(select(M.Slot)))) == 1
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(web.rate_ok, "parallel-test", limit=5) for _ in range(10)]
                assert sum(f.result(timeout=15) for f in futures) == 5
    finally:
        web.app.dependency_overrides.pop(web.get_db, None)
