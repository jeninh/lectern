"""Job runners the desktop agent adds to :class:`lectern.align.worker.RemoteWorker`.

- :class:`KokoroRunner`: ``tts`` jobs with ``payload.engine == "kokoro"`` (capability
  ``tts-kokoro``), the wire contract in docs/contracts.md ("Generated audiobooks: the ``tts``
  kind"). The server owns everything around it (the script, validation, the edition, the
  sentence map); this side only turns sentences into audio with exact timings.
- :class:`TranscodeRunner`: ``transcode`` jobs (data-saver renditions) with ffmpeg.

Both upload outputs as artifacts as soon as they exist, so a retried or resumed attempt
skips work an earlier attempt finished.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from lectern.align.worker import JobContext, PermanentJobError

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

    code = KOKORO_LANGS.get((lang or "en-us").lower())
    if code is None:
        raise PermanentJobError(f"Kokoro has no voice for language {lang!r}")
    pipeline = KPipeline(lang_code=code)

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
        pause = int(round(float(s.get("pause_s", DEFAULT_PAUSE_S)) * sample_rate))
        if pause > 0:
            chunks.append(np.zeros(pause, dtype=np.float32))
            pos += pause
        if on_sentence:
            on_sentence()
    samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return samples, timings


def encode(samples: np.ndarray, sample_rate: int, dest: Path, codec: str, kbps: int,
           ffmpeg: str = "ffmpeg") -> float:  # fmt: skip
    """Encode float32 mono PCM to ``dest`` (AAC in M4A, or FLAC); returns the duration.
    The encoder must not trim: sentence times are relative to the first sample."""
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    args = [ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(sample_rate),
            "-ac", "1", "-i", "pipe:0"]  # fmt: skip
    if codec == "flac":
        args += ["-c:a", "flac", "-f", "flac"]
    else:
        args += ["-c:a", "aac", "-b:a", f"{kbps}k", "-movflags", "+faststart", "-f", "mp4"]
    res = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*args, str(dest)], input=pcm, capture_output=True, timeout=3600, check=False
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode(errors="replace")[-500:] or "ffmpeg failed")
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
        kbps = int(audio.get("bitrate_kbps") or 64)
        rate = int(audio.get("sample_rate") or KOKORO_SAMPLE_RATE)
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
                synth = self._synthesizer(p.get("voice") or "af_heart",
                                          float(p.get("speed") or 1.0), p.get("lang") or "en-us")  # fmt: skip

            def tick(idx=idx) -> None:
                nonlocal done
                done += 1
                ctx.progress(done / total, f"chapter {idx + 1}: {done}/{total} sentences")

            samples, timings = render_chapter(sentences, synth, rate, tick)
            path = ctx.tmp / audio_name
            duration = encode(samples, rate, path, codec, kbps, self.ffmpeg)
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
            "voice": p.get("voice") or "af_heart",
            "speed": float(p.get("speed") or 1.0),
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
                res = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    _ffmpeg_args(self.ffmpeg, src, dest, codec, kbps),
                    capture_output=True, timeout=6 * 3600, check=False,
                )  # fmt: skip
                if res.returncode != 0:
                    raise RuntimeError(res.stderr.decode(errors="replace")[-500:])
                digest = ctx.upload_artifact(out, dest)
                src.unlink(missing_ok=True)
                dest.unlink(missing_ok=True)
            else:
                digest = have[out]["sha256"]
            renditions.append({"track_id": t["track_id"], "sha256": t["sha256"],
                               "artifact": out, "artifact_sha256": digest})  # fmt: skip
        ctx.hb.stop()
        ctx.submit(json.dumps({"renditions": renditions}).encode())
