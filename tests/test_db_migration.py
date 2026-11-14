from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import db as D, models as M


@pytest.mark.parametrize("old,expected", [("6", "1"), ("2", "2")])
def test_upgrade_adds_constraints_and_moves_old_default_once(tmp_path, old, expected):
    engine = create_engine(f"sqlite:///{tmp_path}/legacy.db")
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE slots (id INTEGER PRIMARY KEY, day DATE NOT NULL, '
                          'streamer_id INTEGER NOT NULL, shop_id INTEGER NOT NULL, start DATETIME NOT NULL, '
                          '"end" DATETIME, status VARCHAR(20) NOT NULL, note VARCHAR(200) NOT NULL)'))
        conn.execute(text("CREATE TABLE settings (key VARCHAR(60) PRIMARY KEY, value VARCHAR(200) NOT NULL)"))
        conn.execute(text("INSERT INTO settings (key, value) VALUES ('auto_close_hours', :old)"), {"old": old})
    sessions = sessionmaker(bind=engine)
    with patch.object(D, "engine", engine), patch.object(D, "SessionLocal", sessions):
        D.init_db()
        with sessions() as db:
            assert db.get(M.Setting, "auto_close_hours").value == expected
            assert db.query(M.User).one().name == "Admin"
            db.get(M.Setting, "auto_close_hours").value = "6"
            db.commit()
        D.init_db()
        with sessions() as db:
            assert db.get(M.Setting, "auto_close_hours").value == "6"
        with engine.connect() as conn:
            indexes = conn.execute(text("PRAGMA index_list(slots)")).fetchall()
            assert any(row[1] == "one_open_slot_per_shop" and row[2] == 1 for row in indexes)
    engine.dispose()
