"""SQLAlchemy models. Secrets are never stored in plaintext: see security/tokens.py."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
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
