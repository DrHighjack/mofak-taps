import os, tempfile
os.environ["DATABASE_URL"] = "sqlite://"          # in-memory
os.environ["DISABLE_JOBS"] = "1"
os.environ["ADMIN_PASSWORD"] = "pw"
os.environ["SECRET_KEY"] = "test-only-signing-key-not-for-deployment"
import pytest
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from app import db as D
from app import models as M

# one shared in-memory engine for the whole test session
D.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
D.SessionLocal.configure(bind=D.engine)


@pytest.fixture(autouse=True)
def fresh_db():
    M.Base.metadata.drop_all(D.engine)
    D.init_db()
    with D.SessionLocal() as db:
        db.add_all([M.Shop(name="Shop A", sort=0), M.Shop(name="Shop B", sort=1)])
        db.add_all([M.Streamer(name=n, sort=i) for i, n in enumerate(["Streamer A", "Streamer B", "Streamer C"])])
        db.commit()
    yield


@pytest.fixture
def db():
    with D.SessionLocal() as s:
        yield s


def get(db, model, name):
    from sqlalchemy import select
    return db.scalars(select(model).where(model.name == name)).one()
