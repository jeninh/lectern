"""SQLAlchemy models. Secrets are never stored in plaintext: see security/tokens.py."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """SQLite drops tzinfo; store naive UTC and hand back aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Role(enum.StrEnum):
    OWNER = "owner"
    MEMBER = "member"
    READER = "reader"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=16))
    password_hash: Mapped[str | None] = mapped_column(String(256), default=None)
    totp_secret: Mapped[str | None] = mapped_column(String(64), default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    webauthn_user_handle: Mapped[bytes] = mapped_column(LargeBinary(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # Migration 0032: Home shows a highlight of the day (issue #83).
    quote_of_the_day: Mapped[bool] = mapped_column(Boolean, default=True)
    # UI preference: reveal KOReader xpointers, partial-MD5 ids, and storage paths.
    show_technical_details: Mapped[bool] = mapped_column(Boolean, default=False)
    # Owner-set override of LECTERN_DOWNLOAD_CAP_PER_HOUR for this user; NULL = default, 0 = off.
    download_cap_per_hour: Mapped[int | None] = mapped_column(Integer, default=None)
    # Migration 0007: file uploads whose proposed match is certain without asking.
    auto_file_imports: Mapped[bool] = mapped_column(Boolean, default=False)
    # Migration 0017: play and download the data-saver copy on devices that have not chosen.
    data_saver: Mapped[bool] = mapped_column(Boolean, default=False)
    # Migration 0027: the web reader's default typography and theme, only the fields the
    # person changed (routes/api/reader.py); NULL = never saved, the app's defaults apply.
    reader_settings: Mapped[dict | None] = mapped_column(JSON, default=None)
    # Migration 0029: listening speed for books not played yet; NULL until first chosen.
    playback_speed: Mapped[float | None] = mapped_column(Float, default=None)

    passkeys: Mapped[list[Passkey]] = relationship(back_populates="user", cascade="all, delete")
    sync_keys: Mapped[list[SyncKey]] = relationship(back_populates="user", cascade="all, delete")
    app_passwords: Mapped[list[AppPassword]] = relationship(
        back_populates="user", cascade="all, delete"
    )


class Passkey(Base):
    __tablename__ = "passkeys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary(1024), unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary(2048))
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[str | None] = mapped_column(String(128), default=None)
    label: Mapped[str] = mapped_column(String(128), default="Passkey")
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    user: Mapped[User] = relationship(back_populates="passkeys")


class Session(Base):
    """Browser session. token_hash is SHA-256 of the cookie value."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(256), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class SyncKey(Base):
    """Per-device KOReader progress-sync credential. key_hash = SHA-256(md5hex(secret))."""

    __tablename__ = "sync_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    user: Mapped[User] = relationship(back_populates="sync_keys")

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class AppPassword(Base):
    """Per-device OPDS (HTTP Basic) credential. key_hash = SHA-256(secret)."""

    __tablename__ = "app_passwords"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # Set when both credentials were issued together as one device.
    sync_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_keys.id", ondelete="SET NULL"), default=None, index=True
    )
    # Override of LECTERN_DOWNLOAD_DEVICE_CAP_PER_HOUR for this device; NULL = default, 0 = off.
    download_cap_per_hour: Mapped[int | None] = mapped_column(Integer, default=None)
    # The KOReader plugin downloads queued books on its quiet automatic sync
    # (docs/device-queues.md#following-a-shelf).
    auto_download: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="app_passwords")
    # The shelf this device follows, if any.
    follow: Mapped[DeviceFollow | None] = relationship(passive_deletes=True)

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class AIKey(Base):
    """Bearer credential for an AI app (MCP client). key_hash = SHA-256(secret).

    Created by its user in Devices, shown once, revocable. It acts as that user, limited to
    its ``scopes`` (``upload``, ``file``, ``read``, ``organize``, ``reviews``), and authenticates
    only ``/mcp``: sessions, OPDS, and downloads never accept it (lectern.mcp).
    """

    __tablename__ = "ai_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # First characters of the secret, shown so a person can tell keys apart.
    prefix: Mapped[str] = mapped_column(String(16))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    last_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    last_client: Mapped[str | None] = mapped_column(String(128), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class SetupState(Base):
    """Single-row table tracking first-run setup. token_hash = SHA-256(token)."""

    __tablename__ = "setup_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    token_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(128), index=True)  # username or ip
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    success: Mapped[bool] = mapped_column(Boolean)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(256), default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)


class Work(Base):
    """A work is the unit of the library: one title, possibly ebook + audiobook later."""

    __tablename__ = "works"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    sort_title: Mapped[str] = mapped_column(String(512), index=True)
    author: Mapped[str] = mapped_column(String(512), index=True)  # primary author display
    sort_author: Mapped[str] = mapped_column(String(512), index=True)
    # Migration 0018 indexes language, publisher, published, and created_at: library filters
    # and sorts (lectern.filters).
    language: Mapped[str | None] = mapped_column(String(16), default=None, index=True)
    isbn: Mapped[str | None] = mapped_column(String(13), default=None, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    publisher: Mapped[str | None] = mapped_column(String(256), default=None, index=True)
    published: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    dir_path: Mapped[str] = mapped_column(String(1024), unique=True)  # relative to library
    has_cover: Mapped[bool] = mapped_column(Boolean, default=False)
    cover_tint: Mapped[str | None] = mapped_column(String(7), default=None)  # "#rrggbb"
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    # Migration 0007 (add & edit books). ``author``/``sort_author`` stay the display and sort
    # forms of ``authors`` (every author: [{"name", "sort"}], in order) for OPDS and sorting.
    authors: Mapped[list | None] = mapped_column(JSON, default=None)
    series: Mapped[str | None] = mapped_column(String(256), default=None, index=True)
    series_index: Mapped[float | None] = mapped_column(Float, default=None)
    tags: Mapped[list | None] = mapped_column(JSON, default=None)  # [str]
    narrators: Mapped[list | None] = mapped_column(JSON, default=None)  # [str]
    # The EPUB the reader opens and sentences are built from; NULL (or a file that is no longer
    # this work's) = the first one added. No FK: works <-> book_files would be a cycle.
    primary_file_id: Mapped[int | None] = mapped_column(Integer, default=None)
    playback_default: Mapped[str | None] = mapped_column(String(16), default=None)
    readalong_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Migration 0017: whether data-saver renditions may be made for this work (lectern.jobs).
    data_saver_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cover_source: Mapped[str | None] = mapped_column(String(16), default=None)
    # Migration 0010: the generated cover used while there is no cover art (has_cover False).
    # cover_style None = picked from the title/author hash; cover_seed = the editor's shuffle.
    cover_style: Mapped[str | None] = mapped_column(String(16), default=None)
    cover_seed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Optimistic concurrency: every edit is ``UPDATE ... WHERE version = <seen>``.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Trash: hidden from everyone, files under <library>/_trash/, purged after 30 days.
    trashed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None, index=True)
    trashed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    trash_from: Mapped[str | None] = mapped_column(String(1024), default=None)

    files: Mapped[list[BookFile]] = relationship(back_populates="work", cascade="all, delete")


class FileKind(enum.StrEnum):
    EBOOK = "ebook"
    AUDIO = "audio"


class BookFile(Base):
    __tablename__ = "book_files"
    __table_args__ = (UniqueConstraint("work_id", "rel_path", name="uq_bookfile_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    kind: Mapped[FileKind] = mapped_column(Enum(FileKind, native_enum=False, length=16))
    rel_path: Mapped[str] = mapped_column(String(1024))  # relative to work dir
    media_type: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    partial_md5: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # Migration 0018: pages of a PDF (from its text layer) or a comic, filled in by the
    # housekeeping sweep (lectern.services.fill_page_counts); NULL for EPUBs and not yet
    # counted. The ``pages:`` library filter.
    pages: Mapped[int | None] = mapped_column(Integer, default=None)

    work: Mapped[Work] = relationship(back_populates="files")


class ReadingProgress(Base):
    """KOReader progress, keyed by user and the document digest KOReader sends."""

    __tablename__ = "reading_progress"
    __table_args__ = (UniqueConstraint("user_id", "document", name="uq_progress_user_doc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    document: Mapped[str] = mapped_column(String(128), index=True)
    progress: Mapped[str] = mapped_column(Text)
    percentage: Mapped[float] = mapped_column(Float)
    device: Mapped[str] = mapped_column(String(128), default="")
    device_id: Mapped[str] = mapped_column(String(128), default="")
    timestamp: Mapped[int] = mapped_column(Integer)  # unix seconds, server-assigned
    sync_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_keys.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


# --- Audio, alignment, and canonical positions (contracts for milestones 2-4) --------------
#
# A work has one text timeline (spine_index, char offset into the normalised text of that
# spine item; see docs/contracts.md) and one audio timeline (seconds from the start of track
# 0, tracks laid end to end). Sentences are the join between the two.


class AudioTrack(Base):
    """One audio file of a work's audiobook, placed on the work's single audio timeline."""

    __tablename__ = "audio_tracks"
    __table_args__ = (
        UniqueConstraint("work_id", "position", name="uq_audio_track_position"),
        UniqueConstraint("work_id", "rel_path", name="uq_audio_track_path"),
        UniqueConstraint("work_id", "sha256", name="uq_audio_track_sha256"),
        CheckConstraint("position >= 0", name="ck_audio_track_position"),
        CheckConstraint("duration_s >= 0", name="ck_audio_track_duration"),
        CheckConstraint("offset_s >= 0", name="ck_audio_track_offset"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # 0-based play order
    rel_path: Mapped[str] = mapped_column(String(1024))  # relative to the work dir
    media_type: Mapped[str] = mapped_column(String(128))
    codec: Mapped[str] = mapped_column(String(32))  # e.g. "mp3", "aac", "opus"
    duration_s: Mapped[float] = mapped_column(Float)
    offset_s: Mapped[float] = mapped_column(Float)  # start of this track on the work timeline
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # [{"title": str, "start_s": float}], start_s relative to this track.
    chapters: Mapped[list | None] = mapped_column(JSON, default=None)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # Migration 0007: the name a person gave the track (file names are ASCII-folded).
    title: Mapped[str | None] = mapped_column(String(256), default=None)

    work: Mapped[Work] = relationship()


class Sentence(Base):
    """A sentence of the work's text and, once aligned, where it is spoken."""

    __tablename__ = "sentences"
    __table_args__ = (
        UniqueConstraint("work_id", "n", name="uq_sentence_n"),
        CheckConstraint("n >= 0", name="ck_sentence_n"),
        CheckConstraint("char_end > char_start AND char_start >= 0", name="ck_sentence_span"),
        CheckConstraint(
            "(audio_start_s IS NULL) = (audio_end_s IS NULL)", name="ck_sentence_audio_pair"
        ),
        CheckConstraint(
            "audio_end_s IS NULL OR audio_end_s >= audio_start_s", name="ck_sentence_audio_span"
        ),
        CheckConstraint("NOT matched OR audio_start_s IS NOT NULL", name="ck_sentence_matched"),
        Index("ix_sentences_text", "work_id", "spine_index", "char_start"),
        Index("ix_sentences_audio", "work_id", "audio_start_s"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    n: Mapped[int] = mapped_column(Integer)  # 0-based, contiguous, in reading order
    spine_index: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)  # exclusive
    audio_start_s: Mapped[float | None] = mapped_column(Float, default=None)  # work timeline
    audio_end_s: Mapped[float | None] = mapped_column(Float, default=None)
    matched: Mapped[bool] = mapped_column(Boolean, default=False)


class AlignmentStatus(enum.StrEnum):
    PENDING = "pending"  # queued, no result yet
    RUNNING = "running"  # a worker holds the job lease
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # terminal: retries exhausted or non-retryable error
    STALE = "stale"  # succeeded, but the EPUB or audio changed since


class Alignment(Base):
    """The current text<->audio alignment of a work. One row per work; history lives in jobs."""

    __tablename__ = "alignments"
    __table_args__ = (
        CheckConstraint(
            "coverage IS NULL OR (coverage >= 0 AND coverage <= 1)", name="ck_alignment_coverage"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), unique=True)
    model: Mapped[str] = mapped_column(String(64))  # aligner identifier, e.g. "whisperx-large-v3"
    status: Mapped[AlignmentStatus] = mapped_column(
        Enum(AlignmentStatus, native_enum=False, length=16), index=True
    )
    coverage: Mapped[float | None] = mapped_column(Float, default=None)  # matched / total
    sentence_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Inputs the result was computed from; a mismatch with the work's files makes it STALE.
    epub_sha256: Mapped[str] = mapped_column(String(64))
    audio_fingerprint: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class ProgressSource(enum.StrEnum):
    KOREADER = "koreader"
    WEB = "web"
    AUDIO = "audio"


class WorkProgress(Base):
    """Canonical position per user and work, in sentence coordinates.

    ``reading_progress`` keeps KOReader's own document-hash-keyed record; this table is what
    the web reader and audio player read and write, and what KOReader positions are projected
    onto (lectern.positions) once the work has a sentence map.
    """

    __tablename__ = "work_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "work_id", name="uq_work_progress_user_work"),
        CheckConstraint("sentence_n >= 0 AND sentence_offset >= 0", name="ck_work_progress_pos"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    sentence_n: Mapped[int] = mapped_column(Integer)
    sentence_offset: Mapped[int] = mapped_column(Integer, default=0)  # chars into the sentence
    source: Mapped[ProgressSource] = mapped_column(
        Enum(ProgressSource, native_enum=False, length=16)
    )
    raw: Mapped[str] = mapped_column(Text)  # xpointer, "<seconds>", or "<n>:<offset>"
    device: Mapped[str] = mapped_column(String(128), default="")
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    # Migration 0029: the listening speed last used for this work (player, read-along listen
    # mode), written with an audio position; NULL until one is sent.
    speed: Mapped[float | None] = mapped_column(Float, default=None)


class ReadingState(Base):
    """How far one person is with one work, whatever they read or listened on (migration
    0018, ``lectern.reading_state``): the fraction from the latest position written to
    ``reading_progress`` (KOReader, the page viewer) or ``work_progress`` (web reader, audio
    player), when that was, and when the work was first started and first finished.
    Derived: kept up to date on every flush that writes a position. What the Library's reading
    status and the ``status:``, ``progress:``, ``read:``, ``started:``, and ``finished:``
    filters use."""

    __tablename__ = "reading_states"
    __table_args__ = (Index("ix_reading_states_last_read", "user_id", "last_read_at"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    fraction: Mapped[float] = mapped_column(Float, default=0.0)
    last_read_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class ReaderOverride(Base):
    """One person's web-reader settings for one work, over their default
    (``users.reader_settings``): only the fields that differ, e.g. a smaller size for a dense
    edition (migration 0027). No row: the work follows the default."""

    __tablename__ = "reader_overrides"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    settings: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class BookReview(Base):
    """One person's private rating and notes for one work (migration 0030, ``lectern.reviews``,
    issue #84). ``rating`` is in half stars, 1–10 (½ to 5 stars); ``notes`` is Markdown.
    ``prompt_dismissed_at``: when they said "not now" to "Rate it?". Never shown to anyone
    else, owners included, nor over OPDS; over MCP only with the ``reviews`` scope."""

    __tablename__ = "book_reviews"
    __table_args__ = (
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 10", name="ck_book_review_rating"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    rating: Mapped[int | None] = mapped_column(Integer, default=None)
    notes: Mapped[str] = mapped_column(Text, default="")
    prompt_dismissed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ReadThrough(Base):
    """One time one person read one work, with the days they started and finished it
    (migration 0030, ``lectern.reviews``). Dates are days, stored as midnight UTC. Once a
    person has any for a work, these are their started and finished dates for it, in place of
    the ones ``reading_states`` derives; several are re-reads. Either date may be unknown, not
    both."""

    __tablename__ = "read_throughs"
    __table_args__ = (
        CheckConstraint(
            "started_at IS NOT NULL OR finished_at IS NOT NULL", name="ck_read_through_dates"
        ),
        CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="ck_read_through_order",
        ),
        Index("ix_read_throughs_user_work", "user_id", "work_id"),
        Index("ix_read_throughs_user_finished", "user_id", "finished_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# --- Invites ---------------------------------------------------------------------------


class Invite(Base):
    """Single-use owner-issued sign-up link. token_hash = SHA-256(token)."""

    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint("role IN ('MEMBER', 'READER')", name="ck_invite_role"),
        CheckConstraint("(used_at IS NULL) = (used_by IS NULL)", name="ck_invite_used"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=16))
    note: Mapped[str] = mapped_column(String(128), default="")  # who it is for
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    used_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    def usable_at(self, now: datetime) -> bool:
        return self.used_at is None and self.revoked_at is None and now < self.expires_at


# --- Shelves and grants ------------------------------------------------------------------
#
# Visibility rule: owners and members see the whole library; readers see only works on
# shelves granted to them (lectern.access).


class Shelf(Base):
    """A folder in the Library. Shelves nest (``parent_id``; ``NULL`` = top level, at most
    ``lectern.access.MAX_DEPTH`` deep, never a cycle) and a grant on one covers its whole
    subtree. A work may sit on any number of shelves; on none, it is at the Library's root.
    Names are unique among one owner's siblings (checked in ``routes.api.shelves``)."""

    __tablename__ = "shelves"
    # SQLite reflection skips expression indexes, so a later batch_alter_table("shelves")
    # rebuild silently drops this one: such a migration must create it again.
    __table_args__ = (
        Index(
            "uq_shelf_sibling_name",
            "owner_id",
            text("coalesce(parent_id, 0)"),
            "name",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Deleting a shelf deletes its subtree (the API first moves or trashes what was in it).
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("shelves.id", ondelete="CASCADE"), index=True, default=None
    )
    name: Mapped[str] = mapped_column(String(128))
    # Reserved for smart shelves: a JSON rule evaluated instead of shelf_items. NULL = manual.
    rule: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    parent: Mapped[Shelf | None] = relationship(remote_side="Shelf.id", back_populates="children")
    # The database deletes a subtree (ON DELETE CASCADE); the ORM never deletes or detaches
    # children itself: a child moved to the top level (parent None) is not an orphan.
    children: Mapped[list[Shelf]] = relationship(
        back_populates="parent", cascade="save-update, merge", passive_deletes="all"
    )
    items: Mapped[list[ShelfItem]] = relationship(
        back_populates="shelf", cascade="all, delete-orphan", order_by="ShelfItem.position"
    )
    grants: Mapped[list[ShelfGrant]] = relationship(
        back_populates="shelf", cascade="all, delete-orphan"
    )


class ShelfItem(Base):
    __tablename__ = "shelf_items"

    shelf_id: Mapped[int] = mapped_column(
        ForeignKey("shelves.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    shelf: Mapped[Shelf] = relationship(back_populates="items")


class ShelfPermission(enum.StrEnum):
    VIEW = "view"  # see and read the shelf's works
    EDIT = "edit"  # also add and remove works


class ShelfGrant(Base):
    __tablename__ = "shelf_grants"

    shelf_id: Mapped[int] = mapped_column(
        ForeignKey("shelves.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    permission: Mapped[ShelfPermission] = mapped_column(
        Enum(ShelfPermission, native_enum=False, length=16)
    )
    granted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    shelf: Mapped[Shelf] = relationship(back_populates="grants")


# --- Job queue and external workers ---------------------------------------------------------


class WorkerCredential(Base):
    """Bearer credential for an external alignment worker. key_hash = SHA-256(secret).

    Owner-issued, shown once, revocable. It authenticates only the /api/worker/* routes, so a
    leaked worker key can claim jobs and fetch their inputs but never touch user data.
    """

    __tablename__ = "worker_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # Owner-set allowlist of capabilities this key may claim (lectern.queue); NULL = any
    # capability that may leave the server.
    capabilities: Mapped[list | None] = mapped_column(JSON, default=None)
    # Presence, as last reported by the worker on claim or POST /api/worker/status:
    # what it advertised, its state (ready, busy, paused, off), and why it is paused.
    advertised: Mapped[list | None] = mapped_column(JSON, default=None)
    state: Mapped[str | None] = mapped_column(String(16), default=None)
    info: Mapped[dict | None] = mapped_column(JSON, default=None)
    status_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class WorkerEnrollment(Base):
    """One-time code that the worker installer exchanges for a worker credential.

    Owner-issued, short-lived, single use, stored as SHA-256. It carries the label and the
    capability allowlist the credential will get, so the installer needs no other secret.
    """

    __tablename__ = "worker_enrollments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(128))
    capabilities: Mapped[list | None] = mapped_column(JSON, default=None)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    used_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("worker_credentials.id", ondelete="SET NULL"), default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


JOB_ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.LEASED)


def _job_capability(context) -> str:
    from lectern.queue import capability_for

    params = context.get_current_parameters()
    return capability_for(params.get("kind") or "", params.get("payload"))


class Job(Base):
    """Durable job queue. Claims are atomic (UPDATE ... WHERE status='QUEUED') and leased.

    ``payload`` is the input snapshot (for ``align``: epub_sha256, audio_fingerprint, model);
    a result is accepted only while the lease is held and the snapshot still matches the work.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_job_attempts"),
        # A leased job always has a lease token; finished jobs keep their last one so a
        # repeated result upload from the same lease can be answered idempotently.
        CheckConstraint("status <> 'LEASED' OR lease_token_hash IS NOT NULL", name="ck_job_lease"),
        Index("ix_jobs_claim", "status", "run_after", "priority"),
        # At most one live job of a kind per work; finished jobs stay for history.
        Index(
            "uq_jobs_active_work_kind",
            "work_id",
            "kind",
            unique=True,
            sqlite_where=text("status IN ('QUEUED', 'LEASED')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # "align", "transcode", "tts"
    # What a worker must advertise to claim it (lectern.queue.capability_for): "align",
    # "transcode", "tts-kokoro", ... Filled from kind and payload at insert.
    capability: Mapped[str] = mapped_column(String(48), index=True, default=_job_capability)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), default=None, index=True
    )
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus, native_enum=False, length=16))
    priority: Mapped[int] = mapped_column(Integer, default=0)  # lower runs first
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # claims so far
    # Times this job took its worker down with it (an engine that exits the process, a
    # segfault, an out-of-memory kill), as the worker reports them. CRASH_LIMIT of them fail
    # the job for good, whatever attempts are left (lectern.align.jobs).
    crashes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)  # backoff
    payload: Mapped[dict | None] = mapped_column(JSON, default=None)
    result: Mapped[dict | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Lease: who holds it, until when, proven by the lease token handed out at claim time.
    leased_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("worker_credentials.id", ondelete="SET NULL"), default=None
    )
    lease_token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


# --- Resumable browser uploads (contract for milestone 2) --------------------------------------

UPLOAD_CHUNK_MAX_BYTES = 50 * 1024 * 1024  # under common proxy body caps (Cloudflare: 100 MB)


class UploadStatus(enum.StrEnum):
    ACTIVE = "active"  # accepting chunks
    COMPLETED = "completed"  # verified and handed to the inbox
    CANCELLED = "cancelled"  # by the uploader
    EXPIRED = "expired"  # abandoned; partial file swept
    FAILED = "failed"  # checksum or validation failure at finalize


class UploadSession(Base):
    """One resumable upload: bytes are appended in order to ``<data>/uploads/<public_id>.part``.

    ``received_bytes`` is the resume offset the client asks for after an interruption. Finalize
    checks the whole-file SHA-256 and moves the file into the inbox, where the ordinary ingest
    picks it up; the session row then records the inbox file name.
    """

    __tablename__ = "upload_sessions"
    __table_args__ = (
        CheckConstraint("size > 0", name="ck_upload_size"),
        CheckConstraint(
            f"chunk_size > 0 AND chunk_size <= {UPLOAD_CHUNK_MAX_BYTES}",
            name="ck_upload_chunk_size",
        ),
        CheckConstraint(
            "received_bytes >= 0 AND received_bytes <= size", name="ck_upload_received"
        ),
        CheckConstraint(
            "status <> 'COMPLETED' OR (received_bytes = size AND inbox_name IS NOT NULL)",
            name="ck_upload_completed",
        ),
        Index("ix_upload_sessions_user_status", "user_id", "status"),
        Index("ix_upload_sessions_sweep", "status", "expires_at"),
        Index("ix_upload_sessions_idempotency", "user_id", "idempotency_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # opaque URL id
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(600))  # as the browser named it (sanitised)
    media_type: Mapped[str] = mapped_column(String(64))  # application/epub+zip, audio/mpeg, ...
    size: Mapped[int] = mapped_column(BigInteger)  # declared total, fixed at create
    chunk_size: Mapped[int] = mapped_column(Integer)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)  # expected whole-file
    status: Mapped[UploadStatus] = mapped_column(
        Enum(UploadStatus, native_enum=False, length=16), default=UploadStatus.ACTIVE, index=True
    )
    error: Mapped[str | None] = mapped_column(Text, default=None)
    inbox_name: Mapped[str | None] = mapped_column(String(256), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # Migration 0006: client-chosen key that makes POST /api/uploads idempotent per user, and
    # the digest of the last accepted chunk so a retried chunk can be answered without writing.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)
    last_chunk_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    # Migration 0007: finalize stages the file into this import instead of the inbox.
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("imports.id", ondelete="SET NULL"), default=None, index=True
    )


# --- Milestone 2: security alerts, download caps, ingest match review (migration 0006) -------


class Notification(Base):
    """An in-app alert for one user (new sign-in, new device, download burst or cap, ...).

    In-app is the only delivery today; lectern.notify is where e-mail or webhook sinks plug in.
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_unread", "user_id", "read_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # "login.new", "device.new", "download.cap", ...
    severity: Mapped[str] = mapped_column(String(16), default="info")  # "info" | "warning"
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class KnownDevice(Base):
    """A browser that has signed in before, recognised by a long-lived random cookie.

    token_hash = SHA-256 of the cookie value. A sign-in without a recognised cookie is a new
    device and raises an alert.
    """

    __tablename__ = "known_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_ip: Mapped[str | None] = mapped_column(String(64), default=None)


class DownloadEvent(Base):
    """One whole-file transfer (EPUB, original audio track, web-reader copy), for hourly caps.

    ``credential`` names the device: ``session:<id>`` for a browser, ``app_password:<id>`` for an
    OPDS client. Blocked attempts are kept too so the UI can show them.
    """

    __tablename__ = "download_events"
    __table_args__ = (
        Index("ix_download_events_user_at", "user_id", "at"),
        Index("ix_download_events_credential_at", "credential", "at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    credential: Mapped[str] = mapped_column(String(48))
    target: Mapped[str] = mapped_column(String(64))  # "file:3", "track:7"
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)


class MatchSuggestion(Base):
    """Ingest was unsure whether a new work is the same as an existing one; a person decides.

    ``work_id`` is the work the incoming file created; ``candidate_work_id`` the likely match.
    Merging moves the new work's files into the candidate and deletes the new work.
    """

    __tablename__ = "match_suggestions"
    __table_args__ = (
        UniqueConstraint("work_id", "candidate_work_id", name="uq_match_suggestion_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    candidate_work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), index=True
    )
    score: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    resolution: Mapped[str | None] = mapped_column(String(16), default=None)  # merged|dismissed
    # Migration 0019: "ingest" (a new file looked like an existing work) or "library" (the
    # duplicate scan, lectern.duplicates). A dismissed pair, either way round, is never
    # flagged again.
    origin: Mapped[str] = mapped_column(String(16), default="ingest", server_default="ingest")


class MetadataSuggestion(Base):
    """Migration 0019: the automatic metadata pass for one work (lectern.autometa).

    Both the job and its result: ``queued`` → ``running`` → ``ready`` (``fields`` holds
    suggestions for fields the work was missing), ``no_match``, or ``error`` (``error``
    names why); a person then ``applied`` or ``dismissed`` it. Nothing here writes to the
    work; applying is an ordinary edit with a revision.
    """

    __tablename__ = "metadata_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # {field: {"value", "provider", "provider_name", "source_url"}}
    fields: Mapped[dict | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(String(64), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


# --- Add & edit books (migration 0007) -----------------------------------------------------


class ImportStatus(enum.StrEnum):
    UPLOADING = "uploading"  # files are still arriving
    READY = "ready"  # analysed; waiting for a person (or auto-file) to decide
    FILED = "filed"  # files moved into ``work_id``
    DISCARDED = "discarded"  # by the uploader, or expired; staged files deleted
    FAILED = "failed"  # nothing fileable (every file a duplicate or unreadable)


class Import(Base):
    """A proposed addition to the library: one or more uploaded files that become (or join)
    one work after review. Files wait in ``<library>/_staging/<public_id>/`` so filing is a
    rename on the library's filesystem. ``detected`` is what the files say; ``metadata`` is
    the reviewed version (starts as a copy); ``proposal`` is the matcher's suggestion.
    """

    __tablename__ = "imports"
    __table_args__ = (
        Index("ix_imports_user_status", "user_id", "status"),
        Index("ix_imports_idempotency", "user_id", "idempotency_key"),
        # Migration 0018: who uploaded a work (the ``by:`` library filter).
        Index("ix_imports_work_status", "work_id", "status"),
        CheckConstraint("status <> 'FILED' OR work_id IS NOT NULL", name="ck_import_filed"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(256), default="")  # folder or file name
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus, native_enum=False, length=16), default=ImportStatus.UPLOADING
    )
    detected: Mapped[dict | None] = mapped_column(JSON, default=None)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, default=None)
    proposal: Mapped[dict | None] = mapped_column(JSON, default=None)
    # {"action": "new"} or {"action": "join", "work_id": n}; NULL = follow the proposal.
    target: Mapped[dict | None] = mapped_column(JSON, default=None)
    use_cover: Mapped[bool | None] = mapped_column(Boolean, default=None)
    error: Mapped[str | None] = mapped_column(String(64), default=None)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), default=None
    )
    auto_filed: Mapped[bool] = mapped_column(Boolean, default=False)
    # The AI key whose upload_book opened this import: only that key may file it (MCP).
    ai_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_keys.id", ondelete="SET NULL"), default=None
    )
    # Migration 0019: put the filed work on this shelf (email-in's destination shelf). No
    # foreign key: a shelf deleted before filing is skipped with a notice (lectern.imports).
    shelf_id: Mapped[int | None] = mapped_column(Integer, default=None)
    version: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    filed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    files: Mapped[list[ImportFile]] = relationship(
        back_populates="import_", cascade="all, delete-orphan", order_by="ImportFile.id"
    )


class ImportFile(Base):
    """One staged file of an import. ``status``: ok, duplicate (already in the library, see
    ``duplicate_work_id``), or invalid. ``probe`` keeps what ingest needs (tags, chapters)."""

    __tablename__ = "import_files"
    __table_args__ = (UniqueConstraint("import_id", "sha256", name="uq_import_file_sha"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), index=True)
    kind: Mapped[FileKind] = mapped_column(Enum(FileKind, native_enum=False, length=16))
    filename: Mapped[str] = mapped_column(String(600))  # as uploaded (sanitised)
    rel_path: Mapped[str] = mapped_column(String(256))  # inside the staging directory
    media_type: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    partial_md5: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    duplicate_work_id: Mapped[int | None] = mapped_column(Integer, default=None)
    probe: Mapped[dict | None] = mapped_column(JSON, default=None)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    import_: Mapped[Import] = relationship(back_populates="files")


class WorkRevision(Base):
    """One saved edit of a work: the state before and after, so the save can be undone.

    ``before``/``after`` hold only the aspects the save changed (metadata fields, shelves,
    tracks, cover, removed files, folder). Undo applies ``before`` while the work is still at
    ``version_after``; any later change makes it a conflict instead of a silent overwrite.
    """

    __tablename__ = "work_revisions"
    __table_args__ = (Index("ix_work_revisions_batch", "batch_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str] = mapped_column(String(16), default="edit")  # edit | bulk | undo
    batch_id: Mapped[str | None] = mapped_column(String(32), default=None)
    version_before: Mapped[int] = mapped_column(Integer)
    version_after: Mapped[int] = mapped_column(Integer)
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class TrashedFile(Base):
    """A format removed from a work (an EPUB or an audio track), kept for 30 days under
    ``<library>/_trash/files/``. ``row`` is the original database row, for restore."""

    __tablename__ = "trashed_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), default=None, index=True
    )
    kind: Mapped[FileKind] = mapped_column(Enum(FileKind, native_enum=False, length=16))
    rel_path: Mapped[str] = mapped_column(String(1024))  # where it was, relative to the work
    trash_path: Mapped[str] = mapped_column(String(1024))  # relative to the library
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    row: Mapped[dict] = mapped_column(JSON)
    trashed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    trashed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    restored_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class IdempotencyRecord(Base):
    """The stored answer to a write sent with an ``Idempotency-Key`` header, for 24 hours.

    Scoped to the user and the route; the request digest makes reusing a key for a different
    body an error instead of a silent replay of the wrong answer.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("user_id", "scope", "key", name="uq_idempotency_user_scope_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class InstanceSetting(Base):
    """Owner-controlled, server-wide switches (``metadata_lookup_enabled``)."""

    __tablename__ = "instance_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | bool | None] = mapped_column(JSON, default=None)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


# --- Per-device queues (migration 0008) -------------------------------------------------------


class QueueStatus(enum.StrEnum):
    PENDING = "pending"  # waiting in the device's "Queued for this device" feed
    DELIVERED = "delivered"  # that device downloaded the whole EPUB while it was pending
    REMOVED = "removed"  # taken off the queue before delivery


class DeviceFollow(Base):
    """A device (app password) following a shelf: the shelf's books are queued for it by
    themselves (lectern.device_queue.sync_follow). With ``cleanup``, books that leave the
    shelf are dequeued and the KOReader plugin deletes the copies it downloaded. Deleting the
    shelf or the device ends the follow."""

    __tablename__ = "device_follows"

    app_password_id: Mapped[int] = mapped_column(
        ForeignKey("app_passwords.id", ondelete="CASCADE"), primary_key=True
    )
    shelf_id: Mapped[int] = mapped_column(ForeignKey("shelves.id", ondelete="CASCADE"), index=True)
    cleanup: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DeviceQueueItem(Base):
    """A work queued for one OPDS device (its app password).

    One row per device and work: queueing again a delivered or removed work makes the same row
    pending again (the audit log keeps the history), so enqueueing is idempotent and the row
    can be claimed for delivery with one conditional ``UPDATE``. ``user_id`` is the device's
    owner, kept here so every query is scoped by it without a join.
    """

    __tablename__ = "device_queue_items"
    __table_args__ = (
        UniqueConstraint("app_password_id", "work_id", name="uq_device_queue_device_work"),
        Index("ix_device_queue_items_feed", "app_password_id", "status", "position"),
        CheckConstraint(
            "status <> 'DELIVERED' OR delivered_at IS NOT NULL", name="ck_device_queue_delivered"
        ),
        CheckConstraint(
            "status <> 'REMOVED' OR removed_at IS NOT NULL", name="ck_device_queue_removed"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    app_password_id: Mapped[int] = mapped_column(ForeignKey("app_passwords.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    status: Mapped[QueueStatus] = mapped_column(Enum(QueueStatus, native_enum=False, length=16))
    position: Mapped[int] = mapped_column(Integer)  # feed order among the device's items
    queued_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    delivered_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("book_files.id", ondelete="SET NULL"), default=None
    )
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    removed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # Queued by the device's followed shelf rather than by a person. A followed item that was
    # removed with ``removed_by`` NULL left the shelf (clean-up), not a person's hand.
    followed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class DeviceReport(Base):
    """The latest state a KOReader plugin reported for its device (migration 0031,
    :mod:`lectern.device_report`): battery, versions, free space, and the Lectern books on it.
    One row per device (app password), replaced by each newer report. ``books`` is kept as
    sent (``[{"w": work id, "d": document digest, "p": 0..1}]``) and resolved against the
    library, and the device user's visibility, whenever it is read."""

    __tablename__ = "device_reports"

    app_password_id: Mapped[int] = mapped_column(
        ForeignKey("app_passwords.id", ondelete="CASCADE"), primary_key=True
    )
    reported_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # When the plugin read these values (server time: received minus the age it sent).
    as_of: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    battery: Mapped[int | None] = mapped_column(Integer, default=None)  # percent
    charging: Mapped[bool | None] = mapped_column(Boolean, default=None)
    free_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    plugin_version: Mapped[str | None] = mapped_column(String(32), default=None)
    koreader_version: Mapped[str | None] = mapped_column(String(64), default=None)
    books: Mapped[list] = mapped_column(JSON, default=list)


# --- KOReader plugin setup (migration 0011) ---------------------------------------------------


class DeviceBootstrap(Base):
    """One-time setup secret carried by a device's ``lectern.koplugin`` zip.

    Minted each time the device's user downloads the zip (only the SHA-256 is kept, so the
    zip cannot be downloaded again with the same secret), valid for 7 days, exchanged once for
    the device's long-lived credentials (``lectern.koplugin``), and revocable. Downloading a new
    zip or revoking the device revokes every unused one.
    """

    __tablename__ = "device_bootstraps"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    sync_key_id: Mapped[int] = mapped_column(
        ForeignKey("sync_keys.id", ondelete="CASCADE"), index=True
    )
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # The address baked into the zip, e.g. http://100.101.102.103:8080 (the Tailscale IP).
    server_url: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    used_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class DevicePairingCode(Base):
    """A 6-digit code shown in the web app to (re-)pair a device's plugin.

    Valid for 5 minutes, single use, 5 attempts: every wrong code entered for the user counts
    against their one active code, and the fifth wrong one burns it (``revoked_at``). The code
    is stored as SHA-256 with a per-row salt; a code alone is only 20 bits, which is why the
    attempt limit and the rate limits, not the hash, are what protect it.
    """

    __tablename__ = "device_pairing_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    sync_key_id: Mapped[int] = mapped_column(
        ForeignKey("sync_keys.id", ondelete="CASCADE"), index=True
    )
    salt: Mapped[str] = mapped_column(String(32))
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


# --- Generated audiobooks and third-party integrations (migration 0012_tts) ---------------------


class Integration(Base):
    """An owner-configured third-party API key (``provider`` ``elevenlabs``).

    The key is encrypted at rest (``lectern.integrations``: Fernet, key derived from the
    server's secret key) and never returned by the API after it is saved; ``key_hint`` (the
    last four characters) is all anyone sees again. One row per provider; revoking deletes the
    ciphertext and keeps the row for the audit trail.
    """

    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), unique=True)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    key_hint: Mapped[str | None] = mapped_column(String(8), default=None)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    last_tested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    last_test_detail: Mapped[str | None] = mapped_column(String(256), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class IntegrationUsage(Base):
    """What a paid provider was asked to do: one row per billed request (characters sent,
    credits charged). The per-job spending cap and the cost history are read from here."""

    __tablename__ = "integration_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # "tts", "preview"
    # Jobs that resume a failed one share its ``cap_group`` (the first job's id).
    cap_group: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), default=None
    )
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), default=None
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    characters: Mapped[int] = mapped_column(Integer, default=0)
    credits: Mapped[float] = mapped_column(Float, default=0.0)
    # A narration chunk's request: ``pending`` while it is in flight (reserved against the
    # cap), ``billed`` once its audio is cached, ``uncertain`` when the outcome is unknown
    # (a lost response, a crash). Previews are recorded ``billed``.
    request_key: Mapped[str | None] = mapped_column(String(64), default=None)
    state: Mapped[str] = mapped_column(String(16), default="billed")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class GeneratedAudiobook(Base):
    """A work's audiobook edition made by text-to-speech (``lectern.tts``): which engine,
    voice, and settings produced its tracks. One per work; the tracks themselves are ordinary
    ``audio_tracks`` under ``audio/``, and the sentence map comes from the synthesis."""

    __tablename__ = "generated_audiobooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), unique=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), default=None
    )
    engine: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    voice: Mapped[str] = mapped_column(String(128))
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    settings: Mapped[dict | None] = mapped_column(JSON, default=None)
    chapters: Mapped[int] = mapped_column(Integer, default=0)
    characters: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    epub_sha256: Mapped[str] = mapped_column(String(64))
    audio_fingerprint: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# --- The text layer of PDFs (migration 0016_pdf_text) -------------------------------------------


class PdfText(Base):
    """What Lectern read from one PDF's text layer (``lectern.pdftext``).

    ``status``: ``pending`` (not read yet), ``ready`` (a canonical text document exists:
    ``text_sha256`` names it, ``sections`` summarises it, ``pdf_sentences`` holds its sentences
    with their pages and boxes), ``needs_ocr`` (most pages have no usable text: a scan with no
    recognised text yet; once OCR has run, the document is built from its words and
    ``ocr_sha256`` names them), or ``failed`` (the sandboxed reader refused or
    crashed; ``error`` says why). ``version`` is the extractor's
    (:data:`lectern.pdftext.document.DOCUMENT_VERSION`): a newer one reads the file again.
    """

    __tablename__ = "pdf_texts"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("book_files.id", ondelete="CASCADE"), unique=True
    )
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    text_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    pages_with_text: Mapped[int] = mapped_column(Integer, default=0)
    chars: Mapped[int] = mapped_column(Integer, default=0)
    sentence_count: Mapped[int] = mapped_column(Integer, default=0)
    # [{"title", "page_start", "page_end", "from_outline", "sentences", "characters"}]
    sections: Mapped[list | None] = mapped_column(JSON, default=None)
    stats: Mapped[dict | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(String(512), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # The work sentence list ``pdf_sentences.epub_n`` was projected onto (a digest of it), when
    # the work's text comes from an EPUB; NULL = no projection (the PDF is the text, or none yet).
    projected_key: Mapped[str | None] = mapped_column(String(64), default=None)
    # The OCR words file (``derived/ocr-<prefix>.json``, its SHA-256) the text was built from,
    # NULL when it came from the PDF's own text layer (migration 0019_pdf_text_ocr).
    ocr_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class PdfSentence(Base):
    """A sentence of a PDF's canonical text, where it is on the pages, and (for a work whose
    text is an EPUB) the work sentence it is the same text as.

    ``section``/``char_start``/``char_end`` are text coordinates in the PDF's text document;
    ``boxes`` are highlight rectangles ``[[page, x0, y0, x1, y1], …]`` in PDF user space, one
    per line piece; ``page_start``/``page_end`` are 0-based. ``epub_n`` is NULL when the PDF
    itself is the work's text (then ``n`` is the work's sentence number) or when the text was
    not found in the EPUB.
    """

    __tablename__ = "pdf_sentences"
    __table_args__ = (
        UniqueConstraint("file_id", "n", name="uq_pdf_sentence_n"),
        Index("ix_pdf_sentences_page", "file_id", "page_start"),
        Index("ix_pdf_sentences_epub", "file_id", "epub_n"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("book_files.id", ondelete="CASCADE"))
    n: Mapped[int] = mapped_column(Integer)
    section: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    boxes: Mapped[list] = mapped_column(JSON)
    epub_n: Mapped[int | None] = mapped_column(Integer, default=None)


# --- Email-in (migration 0019, issue #67) ----------------------------------------------------


class EmailInbox(Base):
    """A person's email-in address and the senders it accepts (lectern.email_in).

    The address is ``<local>+<token>@<domain>`` of the configured mailbox: one mailbox the
    server polls, with a hard-to-guess tag per person. Mail is accepted only when it is
    addressed to exactly one such tag and its sender passes the sender filter; its book
    attachments then wait for review in that person's imports, like an upload.
    """

    __tablename__ = "email_inboxes"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    token: Mapped[str] = mapped_column(String(32), unique=True)
    # The shelf emailed books are put on when filed. No foreign key on purpose: a deleted
    # shelf must stay visible as "gone" (a notice, then no shelf), not silently become NULL.
    shelf_id: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Annotation(Base):
    """A highlight (and its note) made in KOReader, pulled in by the plugin (migration 0022,
    :mod:`lectern.annotations`, docs/koreader-plugin.md#highlights-and-notes).

    Private to ``user_id``: nobody else sees it, and its owner only while the work its document
    belongs to is visible to them. ``document`` is KOReader's document digest (the partial MD5
    in ``book_files.partial_md5``), so a highlight follows its file through merges and is shown
    once the book is in the library. ``key`` is the stable id of one annotation: a digest of the
    document, KOReader's creation time, and the start position, so the same highlight from two
    syncs or two devices is one row. ``sources`` lists the sync keys whose latest snapshot of the
    document still has it; it is deleted when none does.

    ``loc_*`` place it on the work's text as the web reader opens it: ``loc_sha`` is the SHA-256
    of the primary EPUB it was placed against (NULL: the work has none), ``loc_spine``,
    ``loc_char``, ``loc_len`` the spine item, normalised-text offset, and length (NULL: its
    text was not found there), and ``sentence_n`` the work sentence it starts in.
    """

    __tablename__ = "annotations"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_annotation_user_key"),
        Index("ix_annotations_user_document", "user_id", "document"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    document: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    chapter: Mapped[str | None] = mapped_column(String(512), default=None)
    color: Mapped[str | None] = mapped_column(String(32), default=None)
    drawer: Mapped[str | None] = mapped_column(String(32), default=None)
    pos0: Mapped[str] = mapped_column(Text)
    pos1: Mapped[str | None] = mapped_column(Text, default=None)
    page: Mapped[int | None] = mapped_column(Integer, default=None)  # 1-based, PDFs
    pageno: Mapped[int | None] = mapped_column(Integer, default=None)  # as KOReader showed it
    # KOReader's own times ("YYYY-MM-DD HH:MM:SS", the device's local time), kept as sent.
    created: Mapped[str] = mapped_column(String(32))
    updated: Mapped[str] = mapped_column(String(32))
    device: Mapped[str] = mapped_column(String(128), default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)
    loc_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    loc_spine: Mapped[int | None] = mapped_column(Integer, default=None)
    loc_char: Mapped[int | None] = mapped_column(Integer, default=None)
    loc_len: Mapped[int | None] = mapped_column(Integer, default=None)
    sentence_n: Mapped[int | None] = mapped_column(Integer, default=None)
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    # Migration 0032: the owner's marks for the quotes feed (issue #83); a hidden one leaves
    # the feed and the highlight of the day.
    favourite: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)


class VocabularyWord(Base):
    """A word one person saved from the dictionary (migration 0039, :mod:`lectern.vocabulary`).

    Private to ``user_id``. One row per (user, language, ``lemma_key``): saving the same word
    again only counts it. ``lang`` is the primary language code ("" = unknown); ``lemma_key``
    is :func:`lectern.dictionary.fold` of ``lemma``. ``work_id``/``file_id`` say where it was
    met and go NULL if that book is deleted; the book is shown only while the person can still
    see it. ``spine_index``/``char``/``length`` place it in an EPUB, ``page`` in a PDF, and
    ``sentence_n`` is the work sentence it starts in. ``dictionary`` names the source for
    attribution only.
    """

    __tablename__ = "vocabulary_words"
    __table_args__ = (
        UniqueConstraint("user_id", "lang", "lemma_key", name="uq_vocabulary_user_word"),
        CheckConstraint("seen_count >= 1", name="ck_vocabulary_seen_count"),
        Index("ix_vocabulary_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    lang: Mapped[str] = mapped_column(String(16), default="")
    lemma: Mapped[str] = mapped_column(String(200))
    lemma_key: Mapped[str] = mapped_column(String(200))
    word: Mapped[str] = mapped_column(String(200))
    to: Mapped[str | None] = mapped_column(String(16), default=None)
    definition: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    pos: Mapped[str | None] = mapped_column(String(32), default=None)
    dictionary: Mapped[str | None] = mapped_column(String(120), default=None)
    context: Mapped[str | None] = mapped_column(Text, default=None)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), default=None, index=True
    )
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("book_files.id", ondelete="SET NULL"), default=None
    )
    spine_index: Mapped[int | None] = mapped_column(Integer, default=None)
    char: Mapped[int | None] = mapped_column(Integer, default=None)
    length: Mapped[int | None] = mapped_column(Integer, default=None)
    page: Mapped[int | None] = mapped_column(Integer, default=None)
    sentence_n: Mapped[int | None] = mapped_column(Integer, default=None)
    seen_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
