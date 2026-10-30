from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from lectern import reading_stats
from lectern.db import session_factory
from lectern.models import ReadingActivity, ReadingState, Role, User, Work


def _owner_and_work():
    with session_factory()() as db:
        user = db.scalar(select(User).where(User.username == "owner"))
        work = Work(title="Stats", sort_title="Stats", author="Author", sort_author="Author", dir_path="stats")
        db.add(work)
        db.commit()
        return user.id, work.id


def test_activity_is_conservative_and_audio_is_content_time(app, owner):
    user_id, work_id = _owner_and_work()
    t0 = datetime(2026, 3, 8, 4, 55, tzinfo=UTC)
    with session_factory()() as db:
        user = db.get(User, user_id)
        reading_stats.record(
            db, user=user, work_id=work_id, source="web", before_fraction=0.1,
            after_fraction=0.11, before_at=t0, at=t0 + timedelta(seconds=30),
        )
        # A jump and a backwards/retry write are baselines, not reading time.
        reading_stats.record(
            db, user=user, work_id=work_id, source="web", before_fraction=0.11,
            after_fraction=0.4, before_at=t0, at=t0 + timedelta(seconds=60),
        )
        reading_stats.record(
            db, user=user, work_id=work_id, source="audio", before_fraction=0.4,
            after_fraction=0.41, before_at=t0, at=t0 + timedelta(seconds=1),
            before_audio_s=100, after_audio_s=160,
        )
        db.commit()
        rows = db.query(ReadingActivity).all()
        assert [(r.read_seconds, r.listened_seconds) for r in rows] == [(30, 0), (0, 60)]


def test_year_review_filters_unshared_work_and_uses_timezone(app, owner):
    user_id, work_id = _owner_and_work()
    at = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)  # Dec 31 in Toronto
    with session_factory()() as db:
        user = db.get(User, user_id)
        user.reading_timezone = "America/Toronto"
        db.add(ReadingActivity(user_id=user.id, work_id=work_id, source="web", at=at,
                               from_fraction=0.1, to_fraction=0.11, read_seconds=61))
        db.add(ReadingState(user_id=user.id, work_id=work_id, fraction=1,
                            last_read_at=at, started_at=at, finished_at=at))
        other = User(username="private-reader", role=Role.READER, password_hash="x", webauthn_user_handle=b"x")
        db.add(other)
        db.flush()
        db.add(ReadingActivity(user_id=other.id, work_id=work_id, source="web", at=at,
                               from_fraction=0.1, to_fraction=0.11, read_seconds=61))
        db.commit()
        assert reading_stats.summary(db, user, 2025)["days"][0]["day"] == "2025-12-31"
        assert reading_stats.summary(db, user, 2026)["totals"]["read_seconds"] == 0
        assert reading_stats.summary(db, other, 2025)["totals"]["read_seconds"] == 0
