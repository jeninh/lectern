"""Runtime configuration. Every knob is an environment variable prefixed LECTERN_."""

from __future__ import annotations

import logging
import secrets
from functools import cached_property, lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lectern.security.proxy import Network, parse_networks

log = logging.getLogger("lectern.config")

# Resource limits (lectern.limits) that fall back to their default when invalid.
LIMIT_FIELDS = tuple(
    f"{feature}_{knob}"
    for feature in ("kokoro", "piper", "ocr", "convert", "align", "transcode")
    for knob in ("concurrency", "threads", "memory_limit_mb", "nice")
    if (feature, knob) != ("convert", "threads")
) + ("heavy_jobs_max",)
# The restore drill's knobs (lectern.drill): an invalid value stops the server, naming the key.
DRILL_FIELDS = (
    "restore_drill_days",
    "restore_drill_read_data_parts",
    "restore_drill_sample",
    "restore_drill_timeout_minutes",
)


class Settings(BaseSettings):
    # An empty value means "the default", so the compose file can pass every key through as
    # ${KEY:-} without repeating the defaults here.
    # hide_input_in_errors: a startup error names the key, never the value (it may be a secret).
    model_config = SettingsConfigDict(
        env_prefix="LECTERN_", extra="ignore", env_ignore_empty=True, hide_input_in_errors=True
    )

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

    # Address KOReader devices use, baked into each lectern.koplugin zip (docs/koreader-plugin.md),
    # e.g. http://100.101.102.103:8080. Empty = this machine's Tailscale IP on `port`, detected.
    device_url: str = Field(default="")

    # Opt-in home Wi-Fi access (docs/home-network.md): a second listener on this port, which
    # answers only private (RFC 1918, link-local, ULA) addresses and only the device protocols
    # (OPDS, KOReader sync, the plugin). 0 = off. deploy/tailscale/lan.yaml publishes it on the
    # host's LAN address; never forward it on the router.
    lan_port: int = Field(default=0, ge=0, le=65535)
    # The address e-readers use at home, e.g. http://192.168.1.20:8080 or
    # http://lectern.local:8080; put into plugin zips next to the Tailscale address.
    lan_url: str = Field(default="")

    # Comma-separated IPs/CIDRs of reverse proxies whose X-Forwarded-For is believed, e.g.
    # "127.0.0.1" for `tailscale serve` on the same host or in a sidecar sharing the network
    # namespace. Empty = trust nobody (default).
    trusted_proxies: str = Field(default="")
    # Believe CF-Connecting-IP from a trusted peer. Only for the Cloudflare Tunnel alternative,
    # with trusted_proxies limited to the cloudflared connector; every other proxy passes the
    # header through untouched, so it would be client-chosen.
    trust_cf_connecting_ip: bool = Field(default=False)

    session_ttl_seconds: int = 60 * 60 * 24 * 30
    # How long the installed app keeps downloads and the signed-in identity without reaching the
    # server (docs/offline.md#privacy). Never past the session's own expiry.
    offline_max_age_seconds: int = 60 * 60 * 24 * 14
    setup_token_ttl_seconds: int = 60 * 30

    # Login protection
    login_max_failures: int = 5
    login_lockout_seconds: int = 60 * 15
    rate_limit_per_minute: int = 60
    # Per worker credential: a job downloads its EPUB and every track, often in ranges.
    worker_requests_per_minute: int = 1200

    # AI apps on the MCP endpoint (/mcp), per AI key: requests per minute (every MCP message,
    # upload chunks included), and the tool calls within the window that alert the key's user
    # and the owners (once an hour). 0 turns the alert off.
    mcp_requests_per_minute: int = 240
    mcp_burst_count: int = 200
    mcp_burst_window_seconds: int = 600

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

    # Full-text search inside books (lectern.search, docs/search.md). The index is a derived
    # file, <data>/search.db, kept up to date by a background thread that looks for added,
    # changed, trashed, and restored books every search_poll_seconds (with background_jobs).
    search_poll_seconds: float = Field(default=10.0, ge=0.5)
    # Text indexed per book (all its files together); the rest of a longer book is not found.
    search_max_chars_per_work: int = Field(default=5_000_000, ge=10_000)
    # Searches per minute per person (the Cmd/Ctrl+K palette asks as you type, debounced).
    search_requests_per_minute: int = Field(default=120, ge=1)
    # A search that takes longer than this is stopped and answered "too broad".
    search_time_budget_ms: int = Field(default=2000, ge=50)

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
    # The server's own transcodes (the Raspberry Pi fallback) run one track at a time, at the
    # lowest CPU priority, on one thread, with ffmpeg's address space capped at this many MiB
    # (0 = no cap of our own). External workers use their own limits.
    transcode_memory_limit_mb: int = Field(default=1024, ge=0)
    transcode_threads: int = Field(default=1, ge=1, le=64)
    transcode_nice: int = Field(default=19, ge=0, le=19)

    # Resource limits of the server's own heavy jobs (lectern.limits, docs/resource-limits.md).
    # Defaults suit a Raspberry Pi 4B with 8 GB that also serves the reader and KOReader sync:
    # one heavy job at a time, each feature's child capped and at a low CPU priority. Jobs that
    # external workers run never count. *_CONCURRENCY = 0 leaves that feature to workers
    # (LECTERN_HEAVY_JOBS_MAX is at least 1). An invalid value is logged by name (never the
    # value) and replaced by the default: a typo must not stop the server.
    heavy_jobs_max: int = Field(default=1, ge=1, le=64)
    transcode_concurrency: int = Field(default=1, ge=0, le=16)
    # Kokoro (PyTorch) and Piper (ONNX) run in a supervised child: its resident memory is
    # capped (0 = no cap of our own), it uses at most *_THREADS CPUs, at this nice level.
    kokoro_concurrency: int = Field(default=1, ge=0, le=16)
    kokoro_threads: int = Field(default=2, ge=1, le=64)
    kokoro_memory_limit_mb: int = Field(default=3072, ge=0)
    kokoro_nice: int = Field(default=10, ge=0, le=19)
    piper_concurrency: int = Field(default=1, ge=0, le=16)
    piper_threads: int = Field(default=2, ge=1, le=64)
    piper_memory_limit_mb: int = Field(default=1024, ge=0)
    piper_nice: int = Field(default=10, ge=0, le=19)
    # PDF → EPUB jobs; the memory cap and nice level also apply to MOBI/AZW3 conversion at
    # import (which shares the parser slots instead of counting as a heavy job).
    convert_concurrency: int = Field(default=1, ge=0, le=16)
    convert_memory_limit_mb: int = Field(default=1536, ge=256, le=65536)
    convert_nice: int = Field(default=10, ge=0, le=19)
    ocr_concurrency: int = Field(default=1, ge=0, le=16)
    ocr_nice: int = Field(default=10, ge=0, le=19)
    align_concurrency: int = Field(default=1, ge=0, le=16)
    align_nice: int = Field(default=10, ge=0, le=19)

    # Alignment (text <-> audio). The in-process worker runs only when faster-whisper is
    # installed (`lectern[align]`); a weak host sets LECTERN_ALIGN_LOCAL_WORKER=0 and runs
    # `lectern worker --server …` elsewhere instead.
    align_model: str = "small"
    align_device: str = "cpu"
    align_compute_type: str = "int8"
    align_threads: int = Field(default=0, ge=0, le=64)  # 0 = CTranslate2 default
    # Audio is decoded as a stream and transcribed in windows of this many seconds that
    # overlap by align_overlap_seconds, so memory does not grow with the length of a track.
    align_window_seconds: float = Field(default=300.0, ge=10.0, le=4 * 3600)
    align_overlap_seconds: float = Field(default=15.0, ge=0.0, le=600.0)
    align_local_worker: bool = True
    # The local worker aligns in a child process so that running out of memory or crashing
    # cannot take the server down. The child's resident memory is capped at this many MiB
    # (it is killed and the job failed above it). Unset: 75 % of the memory available to the
    # server (physical RAM or its cgroup limit, whichever is lower); 0: no cap of our own.
    align_memory_limit_mb: int | None = Field(default=None, ge=0)
    # A running alignment whose progress has not moved for this long is reclaimed (retried
    # with backoff, then failed), even if its worker still heartbeats.
    align_stall_seconds: float = Field(default=3600.0, ge=60.0)
    # Lease sweeps, audio rescans, and the local worker. Off = the server only serves.
    background_jobs: bool = True
    worker_poll_seconds: float = 10.0
    job_sweep_seconds: float = 30.0
    track_rescan_seconds: float = 60.0
    # External workers (docs/mac-worker.md). While a worker that advertises a capability is
    # online and not paused, the server's own runners leave that capability's jobs to it for
    # this long after they were queued, then run them anyway (0 = never defer).
    local_defer_seconds: float = Field(default=600.0, ge=0.0)
    # Largest single artifact (a chapter of a generated audiobook, a rendition) a worker may
    # upload to PUT /api/worker/jobs/{id}/artifacts/{name}.
    worker_artifact_max_bytes: int = Field(default=2 * 1024**3, ge=1024)
    # Everything one job's artifacts may occupy, uploads in flight included.
    worker_artifact_job_max_bytes: int = Field(default=8 * 1024**3, ge=1024)
    # Where /worker/install.sh installs the worker from (a pip requirement URL). A private
    # repository needs git access on the Mac (docs/mac-worker.md, "Installing").
    worker_install_source: str = "git+https://github.com/jeninh/lectern"
    # The git ref (commit or tag) of this server's code; the installer pins the worker to it.
    # The image sets it from the LECTERN_COMMIT build argument (docs/mac-worker.md).
    worker_install_ref: str = Field(default="", pattern=r"^[A-Za-z0-9._/-]{0,100}$")
    # How long a worker enrollment code (the one-command installer) stays valid.
    worker_enrollment_ttl_seconds: int = Field(default=3600, ge=60, le=7 * 24 * 3600)

    # Backups (docs/backups.md). Off until the owner turns them on in Admin; the target is
    # deployment configuration only: the compose files mount LECTERN_BACKUP_PATH (host) here.
    backup_dir: Path = Field(default=Path("/backups"))
    # restic repository password; default <data>/backup-restic-password, created on first use.
    backup_restic_password_file: Path | None = None
    restic_path: str = "restic"
    # `lectern restore`: keep replaced library files in <data>/pre-restore-*/library/. Off
    # deletes them once the restore succeeded (they are copied there when the library is on
    # another volume; on a Pi that is the SD card). Data files are always kept.
    restore_keep_replaced: bool = True
    # Restore drill (docs/backups.md, "Restore drill"): every this many days, right after a
    # nightly backup, restore the latest snapshot into scratch space on the backup volume,
    # check it, report on Health, and delete it. 0 = off (the default); 7 = weekly.
    restore_drill_days: int = Field(default=0, ge=0, le=365)
    # `restic check --read-data-subset=k/N`: each drill reads the next 1/N of the pack data, so
    # N drills read it all. 1 = every pack on every drill; 0 = structure only.
    restore_drill_read_data_parts: int = Field(default=20, ge=0, le=10000)
    # Files sampled per kind (book.json and covers, sentence maps and cover backups, library
    # files), restored and checked against their recorded sizes and hashes.
    restore_drill_sample: int = Field(default=20, ge=1, le=1000)
    # A drill that takes longer is stopped and counted as failed.
    restore_drill_timeout_minutes: int = Field(default=120, ge=5, le=24 * 60)

    # Deprecated: the old name of health_disk_warn_percent (one disk threshold for the Health
    # page and Admin → Storage). Only _storage_warn_alias reads it.
    storage_warn_free_percent: float | None = Field(default=None, ge=0.0, le=100.0)

    # Seconds between checks whether the nightly backup is due.
    maintenance_poll_seconds: float = 60.0

    # Generated audiobooks (docs/tts.md). Piper voices are <voice>.onnx + .onnx.json files;
    # unset = <data>/tts/piper. Kokoro runs in the server only with lectern[kokoro] and this
    # switch on (it wants a fast machine); otherwise Kokoro jobs wait for a tts-kokoro worker.
    piper_voices_dir: Path | None = Field(default=None)
    tts_kokoro_local: bool = False
    tts_audio_codec: str = Field(default="aac", pattern="^(aac|flac)$")
    tts_audio_bitrate_kbps: int = Field(default=64, ge=24, le=256)
    tts_local_runner: bool = True
    # Local runners leave a capability's new jobs to an online worker that has it this long.
    local_defer_seconds: float = Field(default=600.0, ge=0.0)
    # Optional: what 1000 ElevenLabs credits cost you, to show a money estimate (0 = credits only).
    elevenlabs_price_per_1000_credits: float = Field(default=0.0, ge=0.0)

    # Offline mode (docs/offline-mode.md): refuse every outbound internet connection from the
    # server. On here, it cannot be turned off in Admin; off, the owner may still turn it on there.
    offline: bool = False

    # Text recognition for scanned PDFs (docs/ocr.md). The server's runner uses Tesseract when
    # it is on the PATH (the image has it); off = OCR jobs wait for a worker that has it.
    ocr_local_runner: bool = True
    # Queue OCR for every scanned PDF by itself (off = only when someone asks for it).
    ocr_auto: bool = True
    tesseract_path: str = "tesseract"
    # Resolution pages are recognised at (at most 40 megapixels a page).
    ocr_dpi: int = Field(default=300, ge=100, le=600)
    # Address-space cap of each page's child (and of Tesseract in it), MiB. One page at a
    # time, so this bounds what OCR can take on a Raspberry Pi.
    ocr_memory_limit_mb: int = Field(default=1536, ge=256, le=65536)
    # Threads Tesseract may use for one page (OMP_THREAD_LIMIT).
    ocr_threads: int = Field(default=1, ge=1, le=64)

    # Email-in (docs/email-in.md): poll one dedicated mailbox (Gmail or Fastmail) over
    # IMAP with TLS and an app password, for books people mail to their own address. Outbound
    # only: no mail server here. Off unless LECTERN_EMAIL_IN=1 and host, user, and password are
    # set. Strings on purpose: lectern.email_in.load() checks them, so a missing or mistyped
    # value keeps email-in off and is named in Admin instead of stopping the server. The
    # password is never logged or shown.
    email_in: str = ""
    email_in_host: str = ""
    email_in_port: str = ""  # default 993
    email_in_user: str = ""
    email_in_password: SecretStr = SecretStr("")
    # The mailbox's address, for people's <local>+<tag>@<domain> addresses (default: the user).
    email_in_address: str = ""
    email_in_mailbox: str = ""  # default INBOX
    email_in_poll_minutes: str = ""  # default 5
    email_in_max_mb: str = ""  # default 25: larger messages and attachments are refused
    # Sender filter: allowlist (default) or blocklist, and its entries (addresses or @domain,
    # comma-separated). Set here, it wins over Admin → Email-in, which then shows it read-only.
    email_in_sender_mode: str = ""
    email_in_senders: str = ""

    # Health page and alerts (docs/health.md). A volume with less free space than these
    # percentages turns the Health page amber / red and alerts the owners.
    health_disk_warn_percent: float = Field(default=10.0, ge=0.0, le=100.0)
    health_disk_critical_percent: float = Field(default=5.0, ge=0.0, le=100.0)
    # A worker that offered a capability (Kokoro on a Mac) and has not been seen for this long
    # turns that capability amber. 12 hours, so a Mac that sleeps overnight does not alert.
    health_worker_grace_minutes: int = Field(default=720, ge=1, le=7 * 24 * 60)
    # Optional push alerts, outbound only (Lectern opens nothing inbound). Off unless one URL
    # is set; never configurable from the web, never logged or shown in full. ntfy: the topic
    # URL (e.g. https://ntfy.your-tailnet.ts.net/lectern) and an optional access token. Generic
    # webhook (Gotify and similar): a URL that takes a JSON POST and an optional bearer token.
    # Paused while offline mode is on.
    alert_ntfy_url: SecretStr = SecretStr("")
    alert_ntfy_token: SecretStr = SecretStr("")
    alert_webhook_url: SecretStr = SecretStr("")
    alert_webhook_token: SecretStr = SecretStr("")

    log_level: str = "INFO"

    @field_validator(*LIMIT_FIELDS, mode="wrap")
    @classmethod
    def _limit_or_default(cls, v, handler, info):
        try:
            return handler(v)
        except ValidationError:
            default = cls.model_fields[info.field_name].default
            # the key only: the value may be anything someone pasted into .env
            log.warning(
                "LECTERN_%s is invalid, using default %s",
                info.field_name.upper(),
                "(automatic)" if default is None else default,
            )
            return default

    @field_validator(*DRILL_FIELDS, mode="wrap")
    @classmethod
    def _drill_key(cls, v, handler, info):
        try:
            return handler(v)
        except ValidationError:
            meta = cls.model_fields[info.field_name].metadata
            lo = next(m.ge for m in meta if hasattr(m, "ge"))
            hi = next(m.le for m in meta if hasattr(m, "le"))
            raise ValueError(
                f"LECTERN_{info.field_name.upper()} must be a whole number from {lo} to {hi}"
            ) from None

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("device_url")
    @classmethod
    def _valid_device_url(cls, v: str) -> str:
        if not v.strip():
            return ""
        from lectern.koplugin import normalize_server_url

        try:
            return normalize_server_url(v)
        except ValueError as exc:
            raise ValueError("LECTERN_DEVICE_URL must look like http://100.x.y.z:8080") from exc

    @field_validator("lan_url")
    @classmethod
    def _valid_lan_url(cls, v: str) -> str:
        if not v.strip():
            return ""
        from lectern.lan import normalize_lan_url

        try:
            return normalize_lan_url(v)
        except ValueError as exc:
            raise ValueError(
                "LECTERN_LAN_URL must be http://<private IP or name.local>:<port>, "
                "e.g. http://192.168.1.20:8080"
            ) from exc

    @model_validator(mode="after")
    def _lan_port_is_separate(self) -> Settings:
        if self.lan_port and self.lan_port == self.port:
            raise ValueError("LECTERN_LAN_PORT must differ from LECTERN_PORT")
        from lectern.lan import is_home_address

        if self.lan_port and self.lan_url and self.device_url and is_home_address(self.device_url):
            raise ValueError(
                "LECTERN_DEVICE_URL must be the Tailscale address while home Wi-Fi access is on; "
                "the home address goes in LECTERN_LAN_URL"
            )
        return self

    @model_validator(mode="after")
    def _storage_warn_alias(self) -> Settings:
        # Must stay above _disk_thresholds_ordered: after-validators run in definition order.
        if self.storage_warn_free_percent is None:
            return self
        # A constant message: never the value.
        log.warning(
            "LECTERN_STORAGE_WARN_FREE_PERCENT is deprecated; rename it to "
            "LECTERN_HEALTH_DISK_WARN_PERCENT, which wins when both are set"
        )
        if "health_disk_warn_percent" not in self.model_fields_set:
            if self.health_disk_critical_percent > self.storage_warn_free_percent:
                raise ValueError(
                    "LECTERN_STORAGE_WARN_FREE_PERCENT (deprecated alias of "
                    "LECTERN_HEALTH_DISK_WARN_PERCENT) must be at least "
                    "LECTERN_HEALTH_DISK_CRITICAL_PERCENT"
                )
            self.health_disk_warn_percent = self.storage_warn_free_percent
        return self

    @model_validator(mode="after")
    def _disk_thresholds_ordered(self) -> Settings:
        if self.health_disk_critical_percent > self.health_disk_warn_percent:
            raise ValueError(
                "LECTERN_HEALTH_DISK_CRITICAL_PERCENT must be at most "
                "LECTERN_HEALTH_DISK_WARN_PERCENT"
            )
        return self

    @property
    def lan_enabled(self) -> bool:
        return self.lan_port > 0

    @model_validator(mode="after")
    def _window_has_room(self) -> Settings:
        if self.align_overlap_seconds > self.align_window_seconds / 3:
            raise ValueError("LECTERN_ALIGN_OVERLAP_SECONDS must be at most a third of the window")
        return self

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
