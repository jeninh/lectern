"""Runtime configuration. Every knob is an environment variable prefixed LECTERN_."""

from __future__ import annotations

import secrets
from functools import cached_property, lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lectern.security.proxy import Network, parse_networks


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LECTERN_", extra="ignore")

    data_dir: Path = Field(default=Path("/data"))
    library_dir: Path = Field(default=Path("/library"))
    inbox_dir: Path | None = Field(default=None, description="Defaults to <library>/_inbox")

    # Public origin the browser uses, e.g. https://lectern.example. Required for passkeys.
    base_url: str = Field(default="http://localhost:8080")
    # Allow non-TLS cookies. Only for local development; passkeys will not work.
    insecure_http: bool = Field(default=False)

    secret_key: str | None = Field(default=None, description="Signing key; generated if unset")

    host: str = "0.0.0.0"  # noqa: S104 - container default; bind narrower via env if needed
    port: int = 8080

    # Comma-separated IPs/CIDRs of reverse proxies whose X-Forwarded-For is believed, e.g.
    # "127.0.0.1" for `tailscale serve` on the same host or in a sidecar sharing the network
    # namespace. Empty = trust nobody (default).
    trusted_proxies: str = Field(default="")
    # Believe CF-Connecting-IP from a trusted peer. Only for the Cloudflare Tunnel alternative,
    # with trusted_proxies limited to the cloudflared connector; every other proxy passes the
    # header through untouched, so it would be client-chosen.
    trust_cf_connecting_ip: bool = Field(default=False)

    session_ttl_seconds: int = 60 * 60 * 24 * 30
    setup_token_ttl_seconds: int = 60 * 30

    # Login protection
    login_max_failures: int = 5
    login_lockout_seconds: int = 60 * 15
    rate_limit_per_minute: int = 60

    # Ingest
    inbox_poll_seconds: float = 5.0
    ingest_enabled: bool = True
    # Largest file a resumable upload session may declare (audiobooks: one M4B can be GBs).
    upload_max_bytes: int = 4 * 1024**3
    # Largest EPUB accepted by the one-request POST /api/books/upload.
    upload_direct_max_bytes: int = 200 * 1024 * 1024
    # Sum of declared sizes of one user's ACTIVE upload sessions.
    upload_quota_bytes: int = 16 * 1024**3
    upload_max_active: int = 5
    upload_session_ttl_seconds: int = 24 * 60 * 60

    # Download caps and alerts. Caps count whole-file transfers (EPUB, original audio, the web
    # reader's copy) in the last hour; 0 disables a cap. Per-user and per-device overrides live
    # on users / app_passwords.
    download_cap_per_hour: int = 120
    download_device_cap_per_hour: int = 60
    # Alert the user and the owner when this many downloads happen within the window.
    download_burst_count: int = 20
    download_burst_window_seconds: int = 600
    # A streamed audiobook (all tracks of a work) counts as one download per credential per
    # hour once this many bytes were actually sent in that hour (seeking and skipping are free).
    stream_meter_bytes: int = 8 * 1024 * 1024

    # Audio
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    # Low-bitrate "data saver" renditions under <work>/derived/audio/. Codec "aac" (M4A, plays
    # everywhere including iOS) or "opus" (smaller, not on older Safari).
    audio_low_codec: str = Field(default="aac", pattern="^(aac|opus)$")
    audio_low_bitrate_kbps: int = Field(default=64, ge=16, le=192)
    # Queue a data-saver rendition automatically after every audiobook import.
    audio_low_auto: bool = False

    log_level: str = "INFO"

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("trusted_proxies")
    @classmethod
    def _valid_networks(cls, v: str) -> str:
        parse_networks(v)  # fail at startup, not on the first request
        return v

    @cached_property
    def trusted_proxy_networks(self) -> list[Network]:
        return parse_networks(self.trusted_proxies)

    @property
    def inbox_path(self) -> Path:
        return self.inbox_dir or (self.library_dir / "_inbox")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lectern.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path}"

    @property
    def rp_id(self) -> str:
        return urlsplit(self.base_url).hostname or "localhost"

    @property
    def is_https(self) -> bool:
        return urlsplit(self.base_url).scheme == "https"

    @property
    def cookie_secure(self) -> bool:
        return not self.insecure_http

    def resolve_secret_key(self) -> str:
        """Return the signing key, generating and persisting one under data_dir if absent."""
        if self.secret_key:
            return self.secret_key
        key_file = self.data_dir / "secret_key"
        if key_file.exists():
            return key_file.read_text().strip()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        key_file.touch(mode=0o600)
        key_file.write_text(key)
        return key


@lru_cache
def get_settings() -> Settings:
    return Settings()
