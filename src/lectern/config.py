"""Runtime configuration. Every knob is an environment variable prefixed LECTERN_."""

from __future__ import annotations

import secrets
from functools import cached_property, lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
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

    # Address KOReader devices use, baked into each lectern.koplugin zip (docs/koreader-plugin.md),
    # e.g. http://100.101.102.103:8080. Empty = this machine's Tailscale IP on `port`, detected.
    device_url: str = Field(default="")

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

    # Alignment (text <-> audio). The in-process worker runs only when faster-whisper is
    # installed (`lectern[align]`); a weak host sets LECTERN_ALIGN_LOCAL_WORKER=0 and runs
    # `lectern worker --server …` elsewhere instead.
    align_model: str = "small"
    align_device: str = "cpu"
    align_compute_type: str = "int8"
    align_threads: int = 0  # 0 = CTranslate2 default
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

    log_level: str = "INFO"

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
