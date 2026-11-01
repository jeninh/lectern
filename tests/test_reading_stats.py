from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from lectern import reading_stats
from lectern.db import session_factory
from lectern.models import ReadingActivity, ReadingState, ReadingStatsDay, Role, User, Work


def _owner_and_work():
    with session_factory()() as db:
        user = db.scalar(select(User).where(User.username == "owner"))
        work = Work(
            title="Stats",
            sort_title="Stats",
            author="Author",
            sort_author="Author",
            dir_path="stats",
        )
        db.add(work)
        db.commit()
        return user.id, work.id


def test_activity_is_conservative_and_audio_is_content_time(app, owner):
    user_id, work_id = _owner_and_work()
    t0 = datetime(2026, 3, 8, 4, 55, tzinfo=UTC)
    with session_factory()() as db:
        user = db.get(User, user_id)
        reading_stats.record(
            db,
            user=user,
            work_id=work_id,
            source="web",
            before_fraction=0.1,
            after_fraction=0.11,
            before_at=t0,
            at=t0 + timedelta(seconds=30),
        )
        # A jump and a backwards/retry write are baselines, not reading time.
        reading_stats.record(
            db,
            user=user,
            work_id=work_id,
            source="web",
            before_fraction=0.11,
            after_fraction=0.4,
            before_at=t0,
            at=t0 + timedelta(seconds=60),
        )
        reading_stats.record(
            db,
            user=user,
            work_id=work_id,
            source="audio",
            before_fraction=0.4,
            after_fraction=0.41,
            before_at=t0,
            at=t0 + timedelta(seconds=1),
            before_audio_s=100,
            after_audio_s=160,
        )
        db.commit()
        rows = db.query(ReadingActivity).all()
        assert [(r.read_seconds, r.listened_seconds) for r in rows] == [(30, 0), (0, 60)]


def test_audio_with_nonnumeric_prior_raw_never_falls_back_to_read_time(app, owner):
    user_id, work_id = _owner_and_work()
    t0 = datetime(2026, 3, 8, 4, 55, tzinfo=UTC)
    with session_factory()() as db:
        user = db.get(User, user_id)
        reading_stats.record(
            db,
            user=user,
            work_id=work_id,
            source="audio",
            before_fraction=0.1,
            after_fraction=0.11,
            before_at=t0,
            at=t0 + timedelta(seconds=30),
            after_audio_s=160,
        )
        db.commit()
        assert db.query(ReadingActivity).count() == 0


def test_current_streak_includes_today_and_crosses_new_year():
    days = {"2025-12-31", "2026-01-01"}
    current, longest = reading_stats._streaks(days, datetime(2026, 1, 1).date())
    assert (current, longest) == (2, 2)


def test_year_review_filters_unshared_work_and_uses_timezone(app, owner):
    user_id, work_id = _owner_and_work()
    at = datetime(2026, 1, 1, 3, 30, tzinfo=UTC)  # Dec 31 in Toronto
    with session_factory()() as db:
        user = db.get(User, user_id)
        user.reading_timezone = "America/Toronto"
        db.add(
            ReadingActivity(
                user_id=user.id,
                work_id=work_id,
                source="web",
                at=at,
                from_fraction=0.1,
                to_fraction=0.11,
                read_seconds=61,
            )
        )
        db.add(ReadingStatsDay(user_id=user.id, work_id=work_id, day="2025-12-31", read_seconds=61))
        db.add(
            ReadingState(
                user_id=user.id,
                work_id=work_id,
                fraction=1,
                last_read_at=at,
                started_at=at,
                finished_at=at,
            )
        )
        other = User(
            username="private-reader",
            role=Role.READER,
            password_hash="x",
            webauthn_user_handle=b"x",
        )
        db.add(other)
        db.flush()
        db.add(
            ReadingActivity(
                user_id=other.id,
                work_id=work_id,
                source="web",
                at=at,
                from_fraction=0.1,
                to_fraction=0.11,
                read_seconds=61,
            )
        )
        db.commit()
        assert reading_stats.summary(db, user, 2025)["days"][0]["day"] == "2025-12-31"
        assert reading_stats.summary(db, user, 2026)["totals"]["read_seconds"] == 0
        assert reading_stats.summary(db, other, 2025)["totals"]["read_seconds"] == 0


def test_summary_uses_visible_daily_aggregates_across_toronto_dst(app, owner):
    user_id, work_id = _owner_and_work()
    with session_factory()() as db:
        user = db.get(User, user_id)
        user.reading_timezone = "America/Toronto"
        db.add_all(
            [
                ReadingStatsDay(
                    user_id=user.id, work_id=work_id, day="2026-03-07", read_seconds=61
                ),
                ReadingStatsDay(
                    user_id=user.id, work_id=work_id, day="2026-03-08", listened_seconds=61
                ),
                ReadingStatsDay(
                    user_id=user.id, work_id=work_id, day="2026-03-09", read_seconds=61
                ),
            ]
        )
        # This raw row is deliberately outside the review year: summary totals must not scan it.
        db.add(
            ReadingActivity(
                user_id=user.id,
                work_id=work_id,
                source="web",
                at=datetime(2025, 1, 1, tzinfo=UTC),
                from_fraction=0,
                to_fraction=0.1,
                read_seconds=999,
            )
        )
        db.commit()
        summary = reading_stats.summary(db, user, 2026)
        assert summary["totals"] == {"read_seconds": 122, "listened_seconds": 61, "pages": 0.0}
        assert summary["streak"]["longest"] == 3
        assert [day["day"] for day in summary["days"]] == ["2026-03-07", "2026-03-08", "2026-03-09"]


def _read(db, user, work_id, at, before=0.1, after=0.11, secs=30):
    reading_stats.record(
        db,
        user=user,
        work_id=work_id,
        source="web",
        before_fraction=before,
        after_fraction=after,
        before_at=at - timedelta(seconds=secs),
        at=at,
        pages=300,
    )


def test_pruning_raw_events_is_off_by_default_and_keeps_summaries_identical(app, owner):
    from lectern.config import Settings

    assert Settings().stats_retention_days == 0
    user_id, work_id = _owner_and_work()
    with session_factory()() as db:
        user = db.get(User, user_id)
        user.reading_timezone = "America/Toronto"
        # Old: a 3-write session that crosses local midnight, and a second day.
        old = datetime(2026, 2, 3, 4, 59, tzinfo=UTC)  # 23:59 Feb 2 in Toronto
        for i in range(3):
            _read(
                db, user, work_id, old + timedelta(minutes=2 * i), 0.1 + i * 0.01, 0.11 + i * 0.01
            )
        _read(db, user, work_id, datetime(2026, 2, 9, 15, 0, tzinfo=UTC))
        # Recent: stays raw.
        now = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        _read(db, user, work_id, now - timedelta(days=1))
        db.commit()
        before = reading_stats.summary(db, user, 2026)
        assert before["longest_session_seconds"] == 90
        assert reading_stats.prune_raw(db, 0, now=now) == 0  # off
        assert db.query(ReadingActivity).count() == 5
        assert reading_stats.prune_raw(db, 90, now=now) == 4
        db.commit()
        assert db.query(ReadingActivity).count() == 1
        assert reading_stats.summary(db, user, 2026) == before
        # Nothing left to prune; running again is harmless.
        assert reading_stats.prune_raw(db, 90, now=now) == 0
        assert reading_stats.summary(db, user, 2026) == before
        assert db.query(ReadingStatsDay).count() >= 3


def test_rebuilding_days_after_a_prune_keeps_frozen_totals(app, owner):
    user_id, work_id = _owner_and_work()
    with session_factory()() as db:
        user = db.get(User, user_id)
        old = datetime(2026, 2, 3, 4, 59, tzinfo=UTC)
        _read(db, user, work_id, old)
        _read(db, user, work_id, datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
        db.commit()
        reading_stats.prune_raw(db, 30, now=datetime(2026, 10, 1, tzinfo=UTC))
        user.reading_timezone = "America/Toronto"
        reading_stats.rebuild_days(db, user)
        db.commit()
        days = {d.day: d.read_seconds for d in db.query(ReadingStatsDay)}
        # The pruned event stays on its original UTC day; the retained one is rebucketed.
        assert days == {"2026-02-03": 30, "2026-09-30": 30}
        assert db.query(ReadingActivity).count() == 1
