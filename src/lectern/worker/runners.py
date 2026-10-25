"""Job runners the desktop agent adds to :class:`lectern.align.worker.RemoteWorker`.

- :class:`KokoroRunner`: ``tts`` jobs with ``payload.engine == "kokoro"`` (capability
  ``tts-kokoro``), the wire contract in docs/contracts.md ("Generated audiobooks: the ``tts``
  kind"). The server owns everything around it (the script, validation, the edition, the
  sentence map); this side only turns sentences into audio with exact timings.
- :class:`TranscodeRunner`: ``transcode`` jobs (data-saver renditions) with ffmpeg.
- :class:`OcrRunner`: ``ocr`` jobs (text recognition of scanned PDFs) with Tesseract.

All of them upload outputs as artifacts as soon as they exist, so a retried or resumed attempt
skips work an earlier attempt finished.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from lectern.align.worker import EngineUnavailable, JobContext, PermanentJobError

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger("lectern.worker.runners")

KOKORO_SAMPLE_RATE = 24_000
DEFAULT_PAUSE_S = 0.3
# Kokoro's language codes by the payload's BCP 47-ish ``lang``.
KOKORO_LANGS = {
    "en-us": "a",
    "en": "a",
    "en-gb": "b",
    "es": "e",
    "fr": "f",
    "fr-fr": "f",
    "hi": "h",
    "it": "i",
    "ja": "j",
    "pt-br": "p",
    "pt": "p",
    "zh": "z",
}
CODECS = {"aac": "m4a", "flac": "flac"}
# Kokoro voice names (af_heart, bm_george, …): a language letter, f/m, a name. Anything else
# is refused: kokoro loads a voice that ends in ".pt" from the local disk with torch.load.
KOKORO_VOICE = re.compile(r"^[abefhijpz][fm]_[a-z]{2,24}$")
SPEED_RANGE = (0.5, 2.0)
PAUSE_MAX_S = 5.0


def kokoro_available() -> bool:
    return importlib.util.find_spec("kokoro") is not None


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    return shutil.which(ffmpeg) is not None


# A synthesizer turns one sentence into float32 mono samples at ``sample_rate``.
Synthesize = Callable[[str], "np.ndarray"]


def kokoro_synthesizer(voice: str, speed: float, lang: str) -> Synthesize:
    """Kokoro-82M through the ``kokoro`` package (PyTorch; uses the Apple GPU through MPS
    when available). One call per sentence, so every sentence's timing is exact."""
    import numpy as np
    from kokoro import KPipeline

    from lectern.tts.engines import KOKORO_REPO, forbid_runtime_installs

    code = KOKORO_LANGS.get((lang or "en-us").lower())
    if code is None:
        raise PermanentJobError(f"Kokoro has no voice for language {lang!r}")
    forbid_runtime_installs()  # a missing spaCy model is an error here, never a pip install
    pipeline = KPipeline(lang_code=code, repo_id=KOKORO_REPO)

    def synth(text: str) -> np.ndarray:
        parts = [np.asarray(audio, dtype=np.float32) for _, _, audio in
                 pipeline(text, voice=voice, speed=speed, split_pattern=None)]  # fmt: skip
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    return synth


def render_chapter(
    sentences: Iterable[dict],
    synth: Synthesize,
    sample_rate: int,
    on_sentence: Callable[[], None] | None = None,
) -> tuple[np.ndarray, list[dict]]:
    """Synthesize a chapter sentence by sentence. Returns the samples and, per sentence,
    ``{n, start_s, end_s}`` relative to the chapter start: the sentence's own audio, then
    ``pause_s`` of silence that belongs to no sentence."""
    import numpy as np

    chunks: list[np.ndarray] = []
    timings: list[dict] = []
    pos = 0
    for s in sentences:
        audio = np.asarray(synth(str(s["text"])), dtype=np.float32).reshape(-1)
        start = pos
        chunks.append(audio)
        pos += len(audio)
        timings.append(
            {
                "n": int(s["n"]),
                "start_s": round(start / sample_rate, 3),
                "end_s": round(pos / sample_rate, 3),
            }  # fmt: skip
        )
        pause_s = min(PAUSE_MAX_S, max(0.0, float(s.get("pause_s", DEFAULT_PAUSE_S))))
        pause = int(round(pause_s * sample_rate))
        if pause > 0:
            chunks.append(np.zeros(pause, dtype=np.float32))
            pos += pause
        if on_sentence:
            on_sentence()
    samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return samples, timings


def encode(samples: np.ndarray, sample_rate: int, dest: Path, codec: str, kbps: int,
           ffmpeg: str = "ffmpeg", check: Callable[[], None] = lambda: None) -> float:  # fmt: skip
    """Encode float32 mono PCM to ``dest`` (AAC in M4A, or FLAC); returns the duration.
    The encoder must not trim: sentence times are relative to the first sample. ffmpeg runs
    supervised (:func:`run_supervised`): pausing the worker stops it at once."""
    import numpy as np

    pcm = dest.with_name(dest.name + ".pcm")
    pcm.write_bytes((np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
    args = [ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(sample_rate),
            "-ac", "1", "-i", str(pcm)]  # fmt: skip
    if codec == "flac":
        args += ["-c:a", "flac", "-f", "flac"]
    else:
        args += ["-c:a", "aac", "-b:a", f"{kbps}k", "-movflags", "+faststart", "-f", "mp4"]
    try:
        run_supervised([*args, str(dest)], check, poll_s=0.5)
    finally:
        pcm.unlink(missing_ok=True)
    return round(len(samples) / sample_rate, 3)


class KokoroRunner:
    kind = "tts"
    capabilities = ("tts-kokoro",)

    def __init__(self, synthesizer: Callable[[str, float, str], Synthesize] | None = None,
                 ffmpeg: str = "ffmpeg") -> None:  # fmt: skip
        self._synthesizer = synthesizer or kokoro_synthesizer
        self.ffmpeg = ffmpeg

    def run(self, ctx: JobContext) -> None:
        p = ctx.payload
        if p.get("engine") != "kokoro":
            raise PermanentJobError(f"this worker runs Kokoro, not {p.get('engine')!r}")
        audio = p.get("audio") or {}
        codec = audio.get("codec") or "aac"
        if codec not in CODECS:
            raise PermanentJobError(f"unsupported codec {codec!r}")
        kbps = min(320, max(16, int(audio.get("bitrate_kbps") or 64)))
        rate = int(audio.get("sample_rate") or KOKORO_SAMPLE_RATE)
        voice = str(p.get("voice") or "af_heart")
        if not KOKORO_VOICE.match(voice):
            raise PermanentJobError(f"not a Kokoro voice: {voice!r:.40}")
        speed = float(p.get("speed") or 1.0)
        if not SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]:
            raise PermanentJobError(f"speed must be {SPEED_RANGE[0]}–{SPEED_RANGE[1]}")
        if rate != KOKORO_SAMPLE_RATE:
            raise PermanentJobError(f"Kokoro renders {KOKORO_SAMPLE_RATE} Hz, not {rate}")
        ctx.progress(0.0, "downloading the script")
        script = json.loads(ctx.input("script").read_text())
        if script.get("version") != 1:
            raise PermanentJobError("unknown script version")
        chapters = script.get("chapters") or []
        total = sum(len(c.get("sentences") or []) for c in chapters) or 1
        have = ctx.artifacts()
        synth = None
        done = 0
        out_chapters = []
        for ch in chapters:
            idx = int(ch["index"])
            audio_name = f"chapter-{idx:04d}.{CODECS[codec]}"
            timing_name = f"chapter-{idx:04d}.json"
            sentences = ch.get("sentences") or []
            if audio_name in have and timing_name in have:
                # an earlier attempt finished this chapter: reuse its timings
                timing = json.loads(
                    ctx.fetch_artifact(timing_name, have[timing_name]["sha256"]).read_text()
                )
                if [s["n"] for s in timing["sentences"]] == [s["n"] for s in sentences]:
                    done += len(sentences)
                    ctx.progress(done / total, f"chapter {idx + 1}: already rendered")
                    out_chapters.append({**timing, "sha256": have[audio_name]["sha256"]})
                    continue
            if synth is None:
                ctx.progress(done / total, "loading Kokoro")
                try:
                    synth = self._synthesizer(voice, speed, p.get("lang") or "en-us")
                except PermanentJobError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the model would not load here
                    raise EngineUnavailable(f"{type(exc).__name__}: {exc}"[:300]) from exc

            def tick(idx=idx) -> None:
                nonlocal done
                done += 1
                ctx.progress(done / total, f"chapter {idx + 1}: {done}/{total} sentences")

            samples, timings = render_chapter(sentences, synth, rate, tick)
            path = ctx.tmp / audio_name
            duration = encode(samples, rate, path, codec, kbps, self.ffmpeg, ctx.check)
            digest = ctx.upload_artifact(audio_name, path)
            path.unlink(missing_ok=True)
            entry = {"index": idx, "title": ch.get("title") or "", "artifact": audio_name,
                     "duration_s": duration, "sentences": timings}  # fmt: skip
            tpath = ctx.tmp / timing_name
            tpath.write_text(json.dumps(entry))
            ctx.upload_artifact(timing_name, tpath)
            out_chapters.append({**entry, "sha256": digest})
        result = {
            "version": 1,
            "engine": "kokoro",
            "model": p.get("model") or "kokoro-82m",
            "voice": voice,
            "speed": speed,
            "sample_rate": rate,
            "codec": codec,
            "chapters": [
                {
                    k: c[k]
                    for k in ("index", "title", "artifact", "sha256", "duration_s", "sentences")
                }  # fmt: skip
                for c in out_chapters
            ],
        }
        ctx.hb.stop()
        ctx.submit(json.dumps(result).encode())


def run_supervised(argv: list[str], check: Callable[[], None], poll_s: float = 1.0,
                   timeout_s: float = 6 * 3600) -> None:  # fmt: skip
    """Run a command, calling ``check`` every ``poll_s`` (it raises when the worker pauses,
    stops, or loses the lease); the process is killed and reaped before that propagates."""
    import tempfile
    import time

    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=err)  # noqa: S603
        deadline = time.monotonic() + timeout_s
        try:
            while proc.poll() is None:
                check()
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{argv[0]} timed out")
                try:
                    proc.wait(timeout=poll_s)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError(err.read()[-500:].decode(errors="replace") or "ffmpeg failed")


class TranscodeRunner:
    """``transcode`` jobs: one data-saver rendition per payload track (lectern.jobs)."""

    kind = "transcode"
    capabilities = ("transcode",)

    def __init__(self, ffmpeg: str = "ffmpeg") -> None:
        self.ffmpeg = ffmpeg

    def run(self, ctx: JobContext) -> None:
        from lectern.jobs import EXT, _ffmpeg_args

        p = ctx.payload
        codec = p.get("codec") or "aac"
        if codec not in EXT:
            raise PermanentJobError(f"unsupported codec {codec!r}")
        kbps = int(p.get("bitrate_kbps") or 64)
        tracks = p.get("tracks") or []
        have = ctx.artifacts()
        renditions = []
        for i, t in enumerate(tracks):
            name = f"track-{t['track_id']}"
            if name not in (ctx.claimed.get("inputs") or {}):
                continue  # replaced on the server since the job was queued
            out = f"{t['sha256']}.{EXT[codec]}"
            if out not in have:
                ctx.progress(i / max(1, len(tracks)), f"track {i + 1} of {len(tracks)}")
                src = ctx.input(name)
                dest = ctx.tmp / out
                run_supervised(_ffmpeg_args(self.ffmpeg, src, dest, codec, kbps), ctx.check)
                digest = ctx.upload_artifact(out, dest)
                src.unlink(missing_ok=True)
                dest.unlink(missing_ok=True)
            else:
                digest = have[out]["sha256"]
            renditions.append({"track_id": t["track_id"], "sha256": t["sha256"],
                               "artifact": out, "artifact_sha256": digest})  # fmt: skip
        ctx.hb.stop()
        ctx.submit(json.dumps({"renditions": renditions}).encode())


class OcrRunner:
    """``ocr`` jobs (scanned PDFs, lectern.ocr.jobs): Tesseract, one page at a time in the
    same sandboxed child the server uses. Every batch of pages is uploaded as an artifact as
    soon as it is done; the server checks them, makes the searchable PDF, and publishes it."""

    kind = "ocr"
    capabilities = ("ocr",)

    def __init__(self, tesseract: str = "tesseract", *, dpi: int | None = None,
                 memory_mb: int = 1536, threads: int = 1) -> None:  # fmt: skip
        self.tesseract = tesseract
        self.dpi = dpi
        self.memory_mb = memory_mb
        self.threads = threads

    def run(self, ctx: JobContext) -> None:
        from lectern.ocr import child, engine
        from lectern.ocr.jobs import (
            BATCH_PAGES,
            batch_name,
            batches,
            failure_message,
            recognise_batch,
        )

        p = ctx.payload
        language = str(p.get("language") or engine.DEFAULT_LANGUAGE)
        version, installed = engine.probe(self.tesseract)
        if version is None:
            raise EngineUnavailable("Tesseract is not installed")
        try:
            missing = engine.missing_languages(language, installed)
        except ValueError as exc:
            raise PermanentJobError(str(exc)) from None
        if missing:
            # A retryable failure, not a hand-back: the server (or another worker) may take
            # it during the backoff, and with nothing that has the language the attempts run
            # out and the job fails with this reason instead of bouncing forever.
            raise RuntimeError(f"Tesseract has no data for {'+'.join(missing)} on this worker")
        total = int(p.get("pages") or 0)
        dpi = int(self.dpi or p.get("dpi") or 300)
        have = ctx.artifacts()
        pdf = None
        done = []
        for first, last in batches(total, int(p.get("batch_pages") or BATCH_PAGES)):
            name = batch_name(first)
            if name not in have:
                if pdf is None:
                    pdf = ctx.input("pdf", ctx.tmp / "book.pdf")

                def on_page(index: int) -> None:
                    ctx.progress((index + 1) / max(1, total), f"page {index + 1} of {total}")

                try:
                    data = recognise_batch(pdf, first, last, tesseract=self.tesseract,
                                           language=language, dpi=dpi, memory_mb=self.memory_mb,
                                           threads=self.threads, on_page=on_page)  # fmt: skip
                except child.OcrError as exc:
                    message, retryable = failure_message(exc, language)
                    if not retryable and exc.code != "language_missing":
                        raise PermanentJobError(message) from None
                    raise RuntimeError(message) from None  # retried with backoff
                out = ctx.tmp / name
                out.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
                digest = ctx.upload_artifact(name, out)
                out.unlink(missing_ok=True)
            else:
                digest = have[name]["sha256"]
            done.append({"first": first, "artifact": name, "artifact_sha256": digest})
        ctx.hb.stop()
        body = {"engine": engine.label(version), "language": language, "batches": done}
        ctx.submit(json.dumps(body).encode())


def tesseract_available(path: str = "tesseract") -> bool:
    from lectern.ocr import engine

    return engine.probe(path)[0] is not None
