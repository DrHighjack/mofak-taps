from seed import seed
from app import models as M


def test_seed_adds_generic_people_and_is_safe_to_repeat_without_fake_slots(db):
    first = seed(db)
    links = {tk.token for tk in first}
    assert db.query(M.Streamer).count() == 6
    assert db.query(M.Shop).count() == 2
    assert db.query(M.Slot).count() == 0
    assert db.query(M.Token).count() == 1
    assert first[0].streamer_id is None
    assert db.query(M.Streamer).filter_by(name="Streamer F").one()
    assert {tk.token for tk in seed(db)} == links
    seed(db, samples=True)
    assert db.query(M.Slot).count() == 3
    seed(db, samples=True)
    assert db.query(M.Slot).count() == 3
