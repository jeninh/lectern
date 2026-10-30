"""Private, conservative reading statistics derived from position writes.

A position is not a stopwatch.  Text/page activity counts only a small, forward movement made
within an active window; audio counts the forward content seconds, never elapsed wall time.
The append-only rows make the daily cache rebuildable when a person changes their IANA zone.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from lectern.access import visible_works
from lectern.models import ReadingActivity, ReadingState, ReadingStatsDay, User, Work, utcnow

ACTIVE_GAP_S = 15 * 60
TEXT_WRITE_CAP_S = 5 * 60
MAX_FORWARD_FRACTION = 0.15
MAX_AUDIO_ADVANCE_S = 30 * 60
STREAK_MIN_SECONDS = 60


def timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def day_at(at: datetime, zone: str | None) -> str:
    return at.astimezone(timezone(zone)).date().isoformat()


def _day_row(db: Session, user_id: int, work_id: int, day: str) -> ReadingStatsDay:
    row = db.scalar(
        select(ReadingStatsDay).where(
            ReadingStatsDay.user_id == user_id, ReadingStatsDay.work_id == work_id, ReadingStatsDay.day == day
        )
    )
    if row is None:
        row = ReadingStatsDay(
            user_id=user_id, work_id=work_id, day=day, read_seconds=0, listened_seconds=0, pages=0.0
        )
        db.add(row)
    return row


def record(
    db: Session,
    *,
    user: User,
    work_id: int,
    source: str,
    before_fraction: float | None,
    after_fraction: float,
    before_at: datetime | None,
    at: datetime | None = None,
    before_audio_s: float | None = None,
    after_audio_s: float | None = None,
    pages: int | None = None,
) -> None:
    """Append a qualifying activity increment and update its daily cache.

    First observations establish only a baseline. Backward moves, large jumps, source handoffs,
    and repeated flushes count nothing. Text time is a conservative active-window estimate;
    audio is the actual forward content timeline advanced.
    """
    if before_fraction is None:
        return
    at = at or utcnow()
    forward = after_fraction - before_fraction
    if forward <= 0 or forward > MAX_FORWARD_FRACTION:
        return
    read_s = listen_s = 0
    if source == "audio":
        if before_audio_s is not None and after_audio_s is not None:
            advance = after_audio_s - before_audio_s
            if 0 < advance <= MAX_AUDIO_ADVANCE_S:
                listen_s = round(advance)
    elif before_at is not None:
        elapsed = (at - before_at).total_seconds()
        if 0 < elapsed <= ACTIVE_GAP_S:
            read_s = round(min(elapsed, TEXT_WRITE_CAP_S))
    if not (read_s or listen_s):
        return
    page_estimate = forward * pages if pages else 0.0
    db.add(
        ReadingActivity(
            user_id=user.id,
            work_id=work_id,
            source=source,
            at=at,
            from_fraction=before_fraction,
            to_fraction=after_fraction,
            read_seconds=read_s,
            listened_seconds=listen_s,
            pages=page_estimate,
        )
    )
    daily = _day_row(db, user.id, work_id, day_at(at, user.reading_timezone))
    daily.read_seconds = (daily.read_seconds or 0) + read_s
    daily.listened_seconds = (daily.listened_seconds or 0) + listen_s
    daily.pages = (daily.pages or 0.0) + page_estimate


def rebuild_days(db: Session, user: User) -> None:
    """Rebucket all retained raw activity after a timezone edit.

    Raw activity is intentionally retained by default. If a future retention job removes it,
    already-created daily/session summaries retain their original local day rather than silently
    pretending they can be rebucketed without timestamps.
    """
    db.execute(delete(ReadingStatsDay).where(ReadingStatsDay.user_id == user.id))
    rows = db.scalars(
        select(ReadingActivity).where(ReadingActivity.user_id == user.id).order_by(ReadingActivity.at)
    )
    for row in rows:
        daily = _day_row(db, user.id, row.work_id, day_at(row.at, user.reading_timezone))
        daily.read_seconds = (daily.read_seconds or 0) + row.read_seconds
        daily.listened_seconds = (daily.listened_seconds or 0) + row.listened_seconds
        daily.pages = (daily.pages or 0.0) + row.pages


def _visible_activity(db: Session, user: User, start: datetime, end: datetime):
    return (
        select(ReadingActivity)
        .join(Work, Work.id == ReadingActivity.work_id)
        .where(
            ReadingActivity.user_id == user.id,
            ReadingActivity.at >= start,
            ReadingActivity.at < end,
            visible_works(user),
        )
    )


def _visible_days(db: Session, user: User, start: str, end: str):
    return db.execute(
        select(ReadingStatsDay, Work.author)
        .join(Work, Work.id == ReadingStatsDay.work_id)
        .where(
            ReadingStatsDay.user_id == user.id,
            ReadingStatsDay.day >= start,
            ReadingStatsDay.day < end,
            visible_works(user),
        )
    )


def _longest_session(db: Session, user: User, start: datetime, end: datetime) -> int:
    """Largest contiguous counted session in a selected local-year range.

    Daily aggregates intentionally do not preserve session boundaries, so this bounded query is
    the only summary field that reads raw rows. It neither assigns time to historic positions
    nor crosses the requested year's local-calendar boundary.
    """
    longest = total = 0
    previous: datetime | None = None
    for row in db.scalars(_visible_activity(db, user, start, end).order_by(ReadingActivity.at)):
        if previous is None or (row.at - previous).total_seconds() > ACTIVE_GAP_S:
            total = 0
        total += row.read_seconds + row.listened_seconds
        longest = max(longest, total)
        previous = row.at
    return longest


def summary(db: Session, user: User, year: int) -> dict:
    zone = timezone(user.reading_timezone)
    start = datetime(year, 1, 1, tzinfo=zone).astimezone(UTC)
    end = datetime(year + 1, 1, 1, tzinfo=zone).astimezone(UTC)
    start_day = f"{year:04d}-01-01"
    end_day = f"{year + 1:04d}-01-01"
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"read_seconds": 0, "listened_seconds": 0, "pages": 0.0})
    by_author: dict[str, float] = defaultdict(float)
    for row, author in _visible_days(db, user, start_day, end_day):
        bucket = by_day[row.day]
        bucket["read_seconds"] += row.read_seconds
        bucket["listened_seconds"] += row.listened_seconds
        bucket["pages"] += row.pages
        by_author[author or "Unknown"] += row.read_seconds + row.listened_seconds
    active = {d for d, total in by_day.items() if total["read_seconds"] + total["listened_seconds"] >= STREAK_MIN_SECONDS}
    # Current streak must not reset on New Year's Day merely because this review is filtered.
    today = datetime.now(zone).date()
    current_days = {
        row.day
        for row, _ in _visible_days(db, user, "0001-01-01", (today + timedelta(days=1)).isoformat())
        if row.read_seconds + row.listened_seconds >= STREAK_MIN_SECONDS
    }
    current, longest = _streaks(active, today, current_days)
    totals = {
        "read_seconds": sum(int(total["read_seconds"]) for total in by_day.values()),
        "listened_seconds": sum(int(total["listened_seconds"]) for total in by_day.values()),
        "pages": round(sum(float(total["pages"]) for total in by_day.values()), 1),
    }
    started, finished = _state_backfill(db, user, start, end)
    return {
        "year": year,
        "timezone": user.reading_timezone or "UTC",
        "totals": totals,
        "started": started,
        "finished": finished,
        "streak": {"current": current, "longest": longest, "minimum_seconds": STREAK_MIN_SECONDS},
        "days": [{"day": d, **{k: round(v, 1) for k, v in total.items()}} for d, total in sorted(by_day.items())],
        "longest_session_seconds": _longest_session(db, user, start, end),
        "top_authors": [
            {"author": author, "seconds": round(seconds)}
            for author, seconds in sorted(by_author.items(), key=lambda item: (-item[1], item[0]))[:5]
        ],
        "estimate": "Text time is an active-window estimate from forward position changes; audio is content seconds advanced.",
    }


def _state_backfill(db: Session, user: User, start: datetime, end: datetime) -> tuple[int, int]:
    # Existing state dates give honest historical started/finished counts only; no duration is
    # manufactured for them. Visibility applies at read time, including after unsharing.
    started = db.scalar(
        select(func.count()).select_from(ReadingState).join(Work).where(
            ReadingState.user_id == user.id,
            ReadingState.started_at >= start,
            ReadingState.started_at < end,
            visible_works(user),
        )
    ) or 0
    finished = db.scalar(
        select(func.count()).select_from(ReadingState).join(Work).where(
            ReadingState.user_id == user.id,
            ReadingState.finished_at >= start,
            ReadingState.finished_at < end,
            visible_works(user),
        )
    ) or 0
    return int(started), int(finished)


def _streaks(days: set[str], today: date, current_days: set[str] | None = None) -> tuple[int, int]:
    parsed = sorted(date.fromisoformat(day) for day in days)
    longest = run = 0
    previous = None
    for item in parsed:
        run = run + 1 if previous and (item - previous).days == 1 else 1
        longest = max(longest, run)
        previous = item
    current = 0
    cursor = today
    current_days = current_days if current_days is not None else days
    while cursor.isoformat() in current_days:
        current += 1
        cursor = date.fromordinal(cursor.toordinal() - 1)
    return current, longest
