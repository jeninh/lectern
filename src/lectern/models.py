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
    # UI preference: reveal KOReader xpointers, partial-MD5 ids, and storage paths.
    show_technical_details: Mapped[bool] = mapped_column(Boolean, default=False)
    # Owner-set override of LECTERN_DOWNLOAD_CAP_PER_HOUR for this user; NULL = default, 0 = off.
    download_cap_per_hour: Mapped[int | None] = mapped_column(Integer, default=None)

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

    user: Mapped[User] = relationship(back_populates="app_passwords")

    @property
    def active(self) -> bool:
        return self.revoked_at is None


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
    language: Mapped[str | None] = mapped_column(String(16), default=None)
    isbn: Mapped[str | None] = mapped_column(String(13), default=None, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    publisher: Mapped[str | None] = mapped_column(String(256), default=None)
    published: Mapped[str | None] = mapped_column(String(32), default=None)
    dir_path: Mapped[str] = mapped_column(String(1024), unique=True)  # relative to library
    has_cover: Mapped[bool] = mapped_column(Boolean, default=False)
    cover_tint: Mapped[str | None] = mapped_column(String(7), default=None)  # "#rrggbb"
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

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
    __tablename__ = "shelves"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_shelf_owner_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # Reserved for smart shelves: a JSON rule evaluated instead of shelf_items. NULL = manual.
    rule: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

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

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


JOB_ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.LEASED)


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
    kind: Mapped[str] = mapped_column(String(32), index=True)  # "align"
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), default=None, index=True
    )
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus, native_enum=False, length=16))
    priority: Mapped[int] = mapped_column(Integer, default=0)  # lower runs first
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # claims so far
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
