"""ElevenLabs, with the owner's own API key ("bring your own key").

Every request goes to one fixed origin, ``https://api.elevenlabs.io``. The origin is a constant
(no setting can point it elsewhere), redirects are refused, and nothing calls it unless a
person is using ElevenLabs right then: testing the key, listing voices or models, a preview,
an estimate, or a generation they confirmed. The server makes no request at startup or in the
background otherwise. What is sent is the key and, for speech, the text of the book being
narrated (the UI says so before anything is sent).

Speech uses ``POST /v1/text-to-speech/{voice}/with-timestamps``: the response carries
per-character start/end times for the audio it returns, so sentence times are the service's
own, not an alignment. Text goes in paragraph-sized chunks (whole sentences, never split) with
the neighbouring text as ``previous_text``/``next_text`` for continuity. Transient failures
(429, 5xx, network) are retried with exponential backoff honouring ``Retry-After``; a rejected
key, missing quota, or bad request is not.
"""

from __future__ import annotations

import base64
import json
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from lectern import __version__, egress

ORIGIN = "https://api.elevenlabs.io"
HOST = "api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"
SAMPLE_RATE = 24000
OUTPUT_FORMAT = f"pcm_{SAMPLE_RATE}"  # 16-bit LE mono, available on every plan
TIMEOUT_S = 120.0
MAX_BODY_BYTES = 64 * 1024 * 1024
RETRIES = 5
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
# Chunks stay well under every model's per-request limit (multilingual v2: 10 000 characters).
CHUNK_MAX_CHARS = 2000
# Credits per character when /v1/models does not say (Flash and Turbo bill half).
_HALF_RATE = ("eleven_flash", "eleven_turbo")

OFFLINE_MESSAGE = "unavailable while Lectern is in offline mode (Admin → Offline mode)"

# (method, path_and_query, headers, body) -> (status, headers, body)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class ElevenLabsError(Exception):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False, status=None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ElevenLabsError("redirect_refused", "ElevenLabs answered with a redirect")


_OPENER = urllib.request.build_opener(
    _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
)


def https_transport(method: str, path: str, headers: dict, body: bytes | None):
    """The only network code: ``ORIGIN + path``, nothing else is reachable through it."""
    if not path.startswith("/v1/"):
        raise ElevenLabsError("bad_path")
    url = ORIGIN + path
    if urllib.parse.urlsplit(url).hostname != HOST:
        raise ElevenLabsError("bad_path")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            data = resp.read(MAX_BODY_BYTES + 1)
            if len(data) > MAX_BODY_BYTES:
                raise ElevenLabsError("too_large", "ElevenLabs sent too much data")
            return resp.status, dict(resp.headers.items()), data
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read(64 * 1024)
    except ElevenLabsError:
        raise
    except (TimeoutError, urllib.error.URLError, OSError, ssl.SSLError) as exc:
        if isinstance(getattr(exc, "reason", exc), egress.OfflineError):
            raise ElevenLabsError("offline", OFFLINE_MESSAGE) from exc
        raise ElevenLabsError(
            "network", f"could not reach ElevenLabs: {exc}", retryable=True
        ) from exc


@dataclass
class Speech:
    pcm: bytes  # 16-bit LE mono at SAMPLE_RATE
    char_start_s: list[float]
    char_end_s: list[float]
    request_id: str | None


class Client:
    def __init__(
        self,
        key: str | Callable[[], str],
        transport: Transport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        # A callable is asked before every attempt, so a key removed mid-narration stops the
        # next request (and any retry) instead of living on in this client.
        self._key = key if callable(key) else (lambda: key)
        self._transport = transport or https_transport
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security

    # -- transport with retries ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        retries=RETRIES,
        retry_if: Callable[[ElevenLabsError], bool] | None = None,
    ):
        headers = {
            "Accept": "application/json",
            "User-Agent": f"Lectern/{__version__} (self-hosted reading server)",
        }
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            rheaders: dict = {}
            if egress.offline():  # retries included: turning offline mode on stops the next one
                raise ElevenLabsError("offline", OFFLINE_MESSAGE)
            headers["xi-api-key"] = self._key()
            try:
                status, rheaders, data = self._transport(method, path, headers, raw)
                if 200 <= status < 300:
                    return rheaders, data
                err = _error(status, data)
            except ElevenLabsError as exc:
                err = exc
            if not err.retryable or attempt >= retries or (retry_if and not retry_if(err)):
                raise err
            attempt += 1
            self._sleep(self._delay(attempt, rheaders))

    def _delay(self, attempt: int, headers: dict) -> float:
        after = {k.lower(): v for k, v in headers.items()}.get("retry-after")
        if after and str(after).strip().isdigit():
            return min(float(after), BACKOFF_MAX_S)
        base = min(BACKOFF_BASE_S * 2 ** (attempt - 1), BACKOFF_MAX_S)
        return base * (1 + self._rng.uniform(-0.2, 0.2))

    def _json(self, method: str, path: str, body: dict | None = None, **kw):
        _, data = self._request(method, path, body, **kw)
        try:
            return json.loads(data)
        except ValueError as exc:
            raise ElevenLabsError("bad_response", "ElevenLabs sent something unexpected") from exc

    # -- account ------------------------------------------------------------------------------

    def subscription(self) -> dict:
        """Quota: ``{tier, characters_used, characters_limit, remaining, resets_at}``."""
        doc = self._json("GET", "/v1/user/subscription", retries=1)
        used = int(doc.get("character_count") or 0)
        limit = int(doc.get("character_limit") or 0)
        reset = doc.get("next_character_count_reset_unix")
        return {
            "tier": str(doc.get("tier") or "")[:32] or None,
            "characters_used": used,
            "characters_limit": limit,
            "remaining": max(0, limit - used),
            "resets_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(reset)))
                if isinstance(reset, int | float)
                else None
            ),
        }

    def voices(self) -> list[dict]:
        doc = self._json("GET", "/v1/voices", retries=1)
        out = []
        for v in doc.get("voices") or []:
            if not isinstance(v, dict) or not v.get("voice_id"):
                continue
            labels = v.get("labels") if isinstance(v.get("labels"), dict) else {}
            out.append(
                {
                    "id": str(v["voice_id"])[:64],
                    "name": str(v.get("name") or v["voice_id"])[:128],
                    "category": str(v.get("category") or "")[:32] or None,
                    "labels": {str(k)[:32]: str(x)[:64] for k, x in list(labels.items())[:8]},
                }
            )
        return out

    def models(self) -> list[dict]:
        doc = self._json("GET", "/v1/models", retries=1)
        out = []
        for m in doc if isinstance(doc, list) else []:
            if not isinstance(m, dict) or not m.get("can_do_text_to_speech"):
                continue
            rates = m.get("model_rates") if isinstance(m.get("model_rates"), dict) else {}
            mult = rates.get("character_cost_multiplier")
            out.append(
                {
                    "id": str(m.get("model_id"))[:64],
                    "name": str(m.get("name") or m.get("model_id"))[:128],
                    "credits_per_character": (
                        float(mult)
                        if isinstance(mult, int | float) and 0 < mult <= 10
                        else credits_per_character(str(m.get("model_id")))
                    ),
                    "languages": [
                        str(lang.get("language_id"))[:16]
                        for lang in (m.get("languages") or [])[:64]
                        if isinstance(lang, dict) and lang.get("language_id")
                    ],
                }
            )
        return out

    # -- speech -------------------------------------------------------------------------------

    def speech(
        self,
        voice_id: str,
        text: str,
        *,
        model_id: str,
        voice_settings: dict | None = None,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> Speech:
        body: dict = {"text": text, "model_id": model_id}
        if voice_settings:
            body["voice_settings"] = voice_settings
        if previous_text:
            body["previous_text"] = previous_text[-1000:]
        if next_text:
            body["next_text"] = next_text[:1000]
        path = (
            f"/v1/text-to-speech/{urllib.parse.quote(voice_id, safe='')}/with-timestamps"
            f"?output_format={OUTPUT_FORMAT}"
        )
        # Speech is billed. Only a 429 (rate limited, nothing generated) is retried here; any
        # other failure may have been charged, and the caller decides (docs/tts.md).
        headers, data = self._request("POST", path, body, retry_if=lambda e: e.status == 429)
        try:
            doc = json.loads(data)
            pcm = base64.b64decode(doc["audio_base64"])
            align = doc.get("alignment") or {}
            starts = [float(x) for x in align.get("character_start_times_seconds") or []]
            ends = [float(x) for x in align.get("character_end_times_seconds") or []]
        except (ValueError, KeyError, TypeError) as exc:
            raise ElevenLabsError("bad_response", "ElevenLabs sent no audio", status=200) from exc
        if len(starts) != len(text) or len(ends) != len(text):
            # The timestamps index the request text; anything else cannot be placed.
            raise ElevenLabsError(
                "bad_alignment", "ElevenLabs timings do not match the text", status=200
            )
        rid = {k.lower(): v for k, v in headers.items()}.get("request-id")
        return Speech(pcm[: len(pcm) - len(pcm) % 2], starts, ends, rid)


def credits_per_character(model_id: str) -> float:
    return 0.5 if model_id.startswith(_HALF_RATE) else 1.0


def _error(status: int, data: bytes) -> ElevenLabsError:
    detail = ""
    code = ""
    try:
        doc = json.loads(data)
        d = doc.get("detail") if isinstance(doc, dict) else None
        if isinstance(d, dict):
            code = str(d.get("status") or d.get("code") or "")
            detail = str(d.get("message") or "")
        elif isinstance(d, str):
            detail = d
    except ValueError:
        pass
    detail = detail[:300]
    if status == 401:
        return ElevenLabsError("key_rejected", "ElevenLabs rejected the API key", status=status)
    if "quota" in code or status == 402:
        return ElevenLabsError(
            "quota_exceeded", detail or "Not enough ElevenLabs credits left", status=status
        )
    if status == 429:
        return ElevenLabsError(
            "rate_limited", detail or "rate limited", retryable=True, status=status
        )
    if status >= 500:
        return ElevenLabsError(
            "unavailable", f"ElevenLabs error {status}", retryable=True, status=status
        )
    return ElevenLabsError("rejected", detail or f"ElevenLabs refused ({status})", status=status)


# --- chunking ---------------------------------------------------------------------------------


def chunks(sentences: list[dict], max_chars: int = CHUNK_MAX_CHARS) -> list[list[dict]]:
    """Group a chapter's sentences into requests: a paragraph (sentences separated by the short
    pause) stays together up to ``max_chars``; a paragraph break ends a chunk once it is half
    full. A sentence is never split."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for s in sentences:
        n = len(s["text"]) + 1
        if cur and size + n > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += n
        if float(s.get("pause_s", 0.3)) > 0.5 and size >= max_chars // 2:
            out.append(cur)
            cur, size = [], 0
    if cur:
        out.append(cur)
    return out


def chunk_text(chunk: list[dict]) -> str:
    return " ".join(s["text"] for s in chunk)
