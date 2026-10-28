"""Canonical reading/listening position per user and work (``work_progress``).

The web reader writes text positions, the audio player writes seconds, and both read back the
same sentence-coordinate position with its text and audio projections. KOReader keeps its own
document-hash-keyed ``reading_progress``; it is reported alongside (``koreader``) so a client
can offer "Kindle, 12 min ago: continue here?" instead of jumping silently.

Before a work is aligned (milestone 3) its sentences carry no audio times. Conversions between
text and audio then fall back to the share of the book's characters vs. the share of its total
duration, and the response says ``"estimated": true``. Once aligned, the conversions are exact.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lectern.models import (
    AudioTrack,
    BookFile,
    FileKind,
    ProgressSource,
    ReadingProgress,
    Sentence,
    SyncKey,
    WorkProgress,
    utcnow,
)
from lectern.positions import (
    SentenceIndex,
    SentencePos,
    TextPos,
    text_to_xpointer,
    xpointer_spine_index,
    xpointer_to_text,
)


def iso(dt) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


class SentenceCache:
    """Per-work :class:`SentenceIndex` plus per-spine text lengths, rebuilt when rows change."""

    _lock = threading.Lock()
    _items: OrderedDict[int, tuple[tuple, WorkText]] = OrderedDict()
    MAX = 32

    @classmethod
    def get(cls, db: Session, work_id: int) -> WorkText:
        version = tuple(
            db.execute(
                select(func.count(Sentence.id), func.max(Sentence.id)).where(
                    Sentence.work_id == work_id
                )
            ).one()
        )
        with cls._lock:
            hit = cls._items.get(work_id)
            if hit and hit[0] == version:
                cls._items.move_to_end(work_id)
                return hit[1]
        rows = db.scalars(select(Sentence).where(Sentence.work_id == work_id)).all()
        wt = WorkText(SentenceIndex(rows))
        with cls._lock:
            cls._items[work_id] = (version, wt)
            while len(cls._items) > cls.MAX:
                cls._items.popitem(last=False)
        return wt

    @classmethod
    def invalidate(cls, work_id: int) -> None:
        with cls._lock:
            cls._items.pop(work_id, None)


class WorkText:
    """A sentence index plus the cumulative character counts used for estimates."""

    def __init__(self, index: SentenceIndex) -> None:
        self.index = index
        self.count = len(index)
        # Character position of each sentence start in "the whole book", for proportions.
        self._cum: list[int] = []
        total = 0
        for i in range(self.count):
            s = index.sentence(i)
            self._cum.append(total)
            total += s.char_end - s.char_start
        self.total_chars = total
        self.spine_len: dict[int, int] = {}
        for i in range(self.count):
            s = index.sentence(i)
            self.spine_len[s.spine_index] = max(self.spine_len.get(s.spine_index, 0), s.char_end)
        self.matched = any(index.sentence(i).matched for i in range(self.count))

    def fraction(self, pos: SentencePos) -> float:
        if not self.total_chars:
            return 0.0
        s = self.index.sentence(pos.n)
        return min(
            1.0, (self._cum[pos.n] + min(pos.offset, s.char_end - s.char_start)) / self.total_chars
        )

    def at_fraction(self, frac: float) -> SentencePos | None:
        if not self.count:
            return None
        target = max(0.0, min(1.0, frac)) * self.total_chars
        lo, hi = 0, self.count - 1
        while lo < hi:  # last sentence whose start <= target
            mid = (lo + hi + 1) // 2
            if self._cum[mid] <= target:
                lo = mid
            else:
                hi = mid - 1
        s = self.index.sentence(lo)
        return SentencePos(lo, min(int(target - self._cum[lo]), s.char_end - s.char_start))

    def text_from_koreader(self, xpointer: str, percentage: float) -> TextPos | None:
        """Best guess at a KOReader position until xpointers are resolved (milestone 4): the
        spine item it names, at the book-wide percentage clamped into that item."""
        if not self.count:
            return None
        spine = xpointer_spine_index(xpointer)
        if spine is None or spine not in self.spine_len:
            pos = self.at_fraction(percentage)
            return self.index.to_text(pos) if pos else None
        before = sum(n for i, n in self.spine_len.items() if i < spine)
        total = sum(self.spine_len.values()) or 1
        char = int(percentage * total) - before
        return TextPos(spine, max(0, min(char, self.spine_len[spine])))


@dataclass
class Timeline:
    total_s: float
    tracks: int


def audio_timeline(db: Session, work_id: int) -> Timeline:
    total, n = db.execute(
        select(
            func.coalesce(func.sum(AudioTrack.duration_s), 0.0), func.count(AudioTrack.id)
        ).where(AudioTrack.work_id == work_id)
    ).one()
    return Timeline(float(total or 0.0), int(n or 0))


def _audio_for(wt: WorkText, tl: Timeline, pos: SentencePos) -> tuple[float | None, bool]:
    """Seconds for a sentence position, and whether that is an estimate."""
    if wt.matched:
        return wt.index.to_time(pos), False
    if tl.total_s > 0:
        return round(wt.fraction(pos) * tl.total_s, 2), True
    return None, False


def from_audio(wt: WorkText, tl: Timeline, t: float) -> tuple[SentencePos | None, bool]:
    if wt.matched:
        return wt.index.at_time(t), False
    if tl.total_s > 0 and wt.count:
        return wt.at_fraction(t / tl.total_s), True
    return None, False


def progress_json(db: Session, user_id: int, work_id: int) -> dict:
    wt = SentenceCache.get(db, work_id)
    tl = audio_timeline(db, work_id)
    row = db.scalar(
        select(WorkProgress).where(WorkProgress.user_id == user_id, WorkProgress.work_id == work_id)
    )
    out: dict = {
        "work_id": work_id,
        "position": None,
        "source": None,
        "raw": None,
        "device": None,
        "received_at": None,
        "text": None,
        "audio_s": None,
        "percent": None,
        "estimated": False,
        # The listening speed last used for this work, on any device; null: never set (the
        # app then uses the person's default, ``preferences.playback_speed``).
        "speed": None,
        "count": wt.count,
        "total_duration_s": tl.total_s,
        "koreader": koreader_candidate(db, user_id, work_id, wt, tl),
    }
    if row is None:
        return out
    out.update(
        source=row.source.value,
        raw=row.raw,
        device=row.device,
        received_at=iso(row.received_at),
        speed=row.speed,
    )
    audio_raw = _raw_seconds(row) if row.source == ProgressSource.AUDIO else None
    if wt.count and row.sentence_n < wt.count:
        pos = SentencePos(row.sentence_n, row.sentence_offset)
        t = wt.index.to_text(pos)
        out["position"] = {"sentence_n": pos.n, "offset": pos.offset}
        out["text"] = {"spine_index": t.spine_index, "char": t.char}
        out["percent"] = pos.n / wt.count
        if audio_raw is not None:
            out["audio_s"] = audio_raw
            out["estimated"] = not wt.matched
        else:
            out["audio_s"], out["estimated"] = _audio_for(wt, tl, pos)
    elif audio_raw is not None:  # audiobook without text
        out["audio_s"] = audio_raw
        if tl.total_s:
            out["percent"] = min(1.0, audio_raw / tl.total_s)
    return out


def _raw_seconds(row: WorkProgress) -> float | None:
    try:
        return float(row.raw)
    except ValueError:
        return None


def koreader_candidate(
    db: Session, user_id: int, work_id: int, wt: WorkText, tl: Timeline
) -> dict | None:
    """KOReader's latest synced position for any EPUB of this work (read-only)."""
    hit = db.execute(
        select(ReadingProgress, SyncKey)
        .join(BookFile, BookFile.partial_md5 == ReadingProgress.document)
        .outerjoin(SyncKey, SyncKey.id == ReadingProgress.sync_key_id)
        .where(
            ReadingProgress.user_id == user_id,
            BookFile.work_id == work_id,
            BookFile.kind == FileKind.EBOOK,
        )
        .order_by(ReadingProgress.updated_at.desc())
        .limit(1)
    ).first()
    if hit is None:
        return None
    p, key = hit
    text = wt.text_from_koreader(p.progress, p.percentage)
    audio_s = None
    if text is not None:
        pos = wt.index.at_text(text)
        if pos is not None:
            audio_s, _ = _audio_for(wt, tl, pos)
    return {
        "source": "koreader",
        "device": p.device or (key.label if key else "KOReader"),
        "received_at": iso(p.updated_at),
        "percent": p.percentage,
        "raw": p.progress,
        "text": {"spine_index": text.spine_index, "char": text.char} if text else None,
        "audio_s": audio_s,
        "estimated": True,
    }


def replay_wins(
    server_at: datetime | None,
    base: datetime | None,
    recorded_at: datetime,
    sent_at: datetime,
    now: datetime,
) -> tuple[bool, datetime]:
    """Whether a position saved while offline may replace the server's, and when it was
    recorded on the server's clock.

    Online, the server keeps whichever write arrives last. A replay arrives late, so it is
    judged by when it was recorded instead: it wins unless the server's position was saved
    after the device last saw it (``base``) *and* after the replay was recorded. The device's
    clock is corrected by the difference between its ``sent_at`` and the server's ``now``, and
    a recording time in the future counts as now.
    """
    effective = min(now, recorded_at + (now - sent_at))
    if server_at is None or (base is not None and server_at <= base):
        return True, effective
    return effective >= server_at, effective


class ProgressError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code


def put_progress(
    db: Session,
    user_id: int,
    work_id: int,
    source: ProgressSource,
    device: str,
    *,
    sentence: tuple[int, int] | None = None,
    audio_s: float | None = None,
    text: tuple[int, int] | None = None,
    raw: str | None = None,
    speed: float | None = None,
) -> None:
    """Last write wins; ``received_at`` is the server clock. ``speed``, when given, replaces
    the work's listening speed; otherwise the stored one stays. Caller commits."""
    wt = SentenceCache.get(db, work_id)
    tl = audio_timeline(db, work_id)
    if sentence is not None:
        if not wt.count:
            raise ProgressError(409, "no_sentences")
        n, off = sentence
        if n >= wt.count:
            raise ProgressError(422, "sentence_out_of_range")
        s = wt.index.sentence(n)
        pos = SentencePos(n, min(off, s.char_end - s.char_start))
        stored_raw = f"{n}:{off}"
    elif audio_s is not None:
        if tl.tracks == 0:
            raise ProgressError(409, "no_audio")
        audio_s = max(0.0, min(audio_s, tl.total_s))
        pos, _ = from_audio(wt, tl, audio_s)
        if pos is None:  # audiobook with no text: keep the seconds only
            pos = SentencePos(0, 0)
        stored_raw = f"{audio_s:.3f}".rstrip("0").rstrip(".")
    else:
        assert text is not None
        if not wt.count:
            raise ProgressError(409, "no_sentences")
        pos = wt.index.at_text(TextPos(*text))
        stored_raw = f"{text[0]}/{text[1]}"
    row = db.scalar(
        select(WorkProgress).where(WorkProgress.user_id == user_id, WorkProgress.work_id == work_id)
    )
    if row is None:
        row = WorkProgress(user_id=user_id, work_id=work_id, sentence_n=0, raw="", source=source)
        db.add(row)
    row.sentence_n, row.sentence_offset = pos.n, pos.offset
    row.source = source
    row.raw = (raw or stored_raw)[:1024]
    row.device = device[:128]
    row.received_at = utcnow()
    if speed is not None:
        row.speed = speed


# --- KOReader xpointers <-> sentence positions (lectern.routes.kosync) -----------------------


def sentence_index(db: Session, work_id: int) -> SentenceIndex:
    return SentenceCache.get(db, work_id).index


def get_progress(db: Session, user_id: int, work_id: int) -> WorkProgress | None:
    return db.scalar(
        select(WorkProgress).where(WorkProgress.user_id == user_id, WorkProgress.work_id == work_id)
    )


def set_progress(
    db: Session,
    *,
    user_id: int,
    work_id: int,
    pos: SentencePos,
    source: ProgressSource,
    raw: str,
    device: str = "",
    now: datetime | None = None,
) -> WorkProgress:
    """Upsert the canonical position from an already-resolved sentence position. Caller
    commits."""
    now = now or utcnow()
    row = get_progress(db, user_id, work_id)
    if row is None:
        row = WorkProgress(
            user_id=user_id, work_id=work_id, sentence_n=pos.n, raw="", source=source
        )
        db.add(row)
    row.sentence_n, row.sentence_offset = pos.n, pos.offset
    row.source = source
    row.raw = raw[:1024]
    row.device = device[:128]
    row.received_at = now
    row.updated_at = now
    db.flush()
    return row


def xpointer_to_sentence(epub: Path, idx: SentenceIndex, xpointer: str) -> SentencePos | None:
    """Project a KOReader position in ``epub`` onto the work's sentences; ``None`` when it
    cannot be. ``epub`` must be the edition the sentences were aligned against."""
    from lectern.align.text import read_spine_item

    if not len(idx):
        return None
    spine_index = xpointer_spine_index(xpointer)
    if spine_index is None:
        return None
    try:
        text = xpointer_to_text(xpointer, read_spine_item(epub, spine_index))
    except Exception:  # noqa: BLE001 - a damaged EPUB must not break sync
        return None
    return idx.at_text(text)


def sentence_to_xpointer(epub: Path, idx: SentenceIndex, pos: SentencePos) -> str | None:
    """Inverse of :func:`xpointer_to_sentence` for the same edition."""
    from lectern.align.text import read_spine_item

    if not len(idx):
        return None
    text: TextPos = idx.to_text(pos)
    try:
        return text_to_xpointer(text, read_spine_item(epub, text.spine_index))
    except Exception:  # noqa: BLE001
        return None
