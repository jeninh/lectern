"""Narrate a tts job's script into chapter artifacts and the result body (the server's runner;
the Mac worker's Kokoro client produces the same files).

Per chapter: synthesise, write raw PCM to a scratch file while counting samples, encode once,
then write ``chapter-NNNN.<ext>`` and ``chapter-NNNN.json`` (that chapter's result entry) to the
job's artifacts directory. A chapter whose two files are already there with matching hashes is
skipped, so a retried or resumed attempt only narrates what is missing.

Timings are synthesis-native:

- Piper and Kokoro speak one sentence per call; a sentence starts at the sample count before
  it and ends where its own samples end, then its ``pause_s`` of silence follows.
- ElevenLabs speaks a paragraph-sized chunk per call and returns a start and end time for
  every character of the request; a sentence runs from its first character's start to its
  last character's end, shifted by the chunk's place in the chapter. Paid chunks are cached
  on disk (keyed by everything that affects the audio), so nothing is billed twice, and each
  one is checked against the job's spending cap before it is requested.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lectern.ingest.hashing import sha256_file
from lectern.tts import audio as tts_audio
from lectern.tts import elevenlabs as el
from lectern.tts.engines import Synth, silence

ProgressFn = Callable[[float, str, dict], None]


class SpendingCapReached(Exception):
    """The next ElevenLabs request would take the job past its confirmed cap."""


class UncertainCharge(Exception):
    """An ElevenLabs request may have been billed but its audio was not received (a lost
    response, a server error, a crash). It is never re-sent automatically: only the owner's
    Retry sends it again, still counted against the cap."""


@dataclass
class ElevenLabsPlan:
    client: el.Client
    model: str
    voice_settings: dict | None
    credits_per_character: float
    cap_credits: float
    spent: Callable[[], float]  # credits billed or reserved for this job (and its retries)
    # reserve(request_key, characters, credits) -> ledger row id: checks the cap and records the
    # request as pending before it is sent (raises SpendingCapReached / UncertainCharge)
    reserve: Callable[[str, int, float], int]
    settle: Callable[[int, str], None]  # (row, "billed" | "uncertain")
    release: Callable[[int], None]  # the request was refused before anything was generated
    cache_dir: Path


@dataclass
class Run:
    payload: dict
    script: dict
    artifacts: Path
    scratch: Path
    ffmpeg: str
    tags: dict = field(default_factory=dict)
    progress: ProgressFn = lambda _f, _m, _s: None
    synth: Synth | None = None  # Piper / Kokoro
    eleven: ElevenLabsPlan | None = None


def chapter_names(index: int, codec: str) -> tuple[str, str]:
    return f"chapter-{index:04d}.{tts_audio.EXT[codec]}", f"chapter-{index:04d}.json"


def _done_chapter(artifacts: Path, index: int, codec: str) -> dict | None:
    audio_name, json_name = chapter_names(index, codec)
    try:
        entry = json.loads((artifacts / json_name).read_text())
    except (OSError, ValueError):
        return None
    path = artifacts / audio_name
    if entry.get("artifact") != audio_name or not path.is_file():
        return None
    return entry if sha256_file(path) == entry.get("sha256") else None


def run(job: Run) -> bytes:
    """Narrate every chapter that is not finished yet; returns the result body (JSON)."""
    p = job.payload
    codec = (p.get("audio") or {}).get("codec", "aac")
    kbps = int((p.get("audio") or {}).get("bitrate_kbps") or 64)
    chapters = job.script["chapters"]
    total = sum(len(c["sentences"]) for c in chapters) or 1
    job.artifacts.mkdir(parents=True, exist_ok=True)
    job.scratch.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    done_sentences = done_chars = resumed = 0
    audio_s = 0.0
    entries = []
    sample_rate = job.synth.sample_rate if job.synth else el.SAMPLE_RATE

    def stats() -> dict:
        elapsed = time.monotonic() - started
        out = {"engine": p.get("engine"), "voice": p.get("voice"), "sentences_done": done_sentences,
               "characters_done": done_chars, "audio_s": round(audio_s, 3),
               "elapsed_s": round(elapsed, 3), "chapters_resumed": resumed}  # fmt: skip
        if elapsed > 0 and done_chars:
            out["chars_per_second"] = round(done_chars / elapsed, 3)
        if job.eleven is not None:
            out["credits"] = round(job.eleven.spent(), 3)
        return out

    for ch in chapters:
        entry = _done_chapter(job.artifacts, ch["index"], codec)
        if entry is None:
            pcm_path = job.scratch / f"chapter-{ch['index']:04d}.pcm"

            def step(n_sentences: int, n_chars: int, title=ch["title"]) -> None:
                nonlocal done_sentences, done_chars
                done_sentences += n_sentences
                done_chars += n_chars
                job.progress(done_sentences / total, f"narrating {title}"[:200], stats())

            with pcm_path.open("wb") as out:
                if job.eleven is not None:
                    timings = _eleven_chapter(job.eleven, p["voice"], ch, out, step)
                else:
                    timings = _local_chapter(job.synth, ch, out, step)
            samples = pcm_path.stat().st_size // 2
            audio_name, json_name = chapter_names(ch["index"], codec)
            tags = {**job.tags, "title": ch["title"], "track": str(ch["index"] + 1)}
            try:
                sha = tts_audio.encode(job.ffmpeg, pcm_path, job.artifacts / audio_name,
                                       sample_rate=sample_rate, codec=codec, bitrate_kbps=kbps,
                                       tags=tags)  # fmt: skip
            finally:
                pcm_path.unlink(missing_ok=True)
            entry = {"index": ch["index"], "title": ch["title"], "artifact": audio_name,
                     "sha256": sha, "duration_s": round(samples / sample_rate, 3),
                     "sentences": timings}  # fmt: skip
            tmp = job.artifacts / f".{json_name}.tmp"
            tmp.write_text(json.dumps(entry))
            os.replace(tmp, job.artifacts / json_name)
        else:
            resumed += 1
            done_sentences += len(ch["sentences"])
            done_chars += sum(len(s["text"]) for s in ch["sentences"])
            job.progress(done_sentences / total, f"resumed {ch['title']}"[:200], stats())
        audio_s += float(entry["duration_s"])
        entries.append(entry)
    job.progress(1.0, "uploading", stats())
    return json.dumps(
        {
            "version": 1,
            "engine": p.get("engine"),
            "model": p.get("model") or "",
            "voice": p.get("voice"),
            "speed": p.get("speed") or 1.0,
            "sample_rate": sample_rate,
            "codec": codec,
            "chapters": entries,
        }  # fmt: skip
    ).encode()


def _local_chapter(synth: Synth, ch: dict, out, step) -> list[dict]:
    sr = synth.sample_rate
    pos = 0  # samples written so far
    timings = []
    for s in ch["sentences"]:
        pcm = synth.sentence(s["text"])
        pcm = pcm[: len(pcm) - len(pcm) % 2]
        start = pos
        out.write(pcm)
        pos += len(pcm) // 2
        timings.append({"n": s["n"], "start_s": round(start / sr, 3), "end_s": round(pos / sr, 3)})
        gap = silence(float(s.get("pause_s", 0.3)), sr)
        out.write(gap)
        pos += len(gap) // 2
        step(1, len(s["text"]))
    return timings


def cache_key(model: str, voice: str, settings: dict | None, text: str, prev: str, nxt: str) -> str:
    doc = json.dumps(
        [model, voice, settings or {}, text, prev, nxt, el.OUTPUT_FORMAT], sort_keys=True
    )
    return hashlib.sha256(doc.encode()).hexdigest()


def _eleven_chapter(plan: ElevenLabsPlan, voice: str, ch: dict, out, step) -> list[dict]:
    sr = el.SAMPLE_RATE
    groups = el.chunks(ch["sentences"])
    pos = 0
    timings = []
    plan.cache_dir.mkdir(parents=True, exist_ok=True)
    for gi, group in enumerate(groups):
        text = el.chunk_text(group)
        prev = el.chunk_text(groups[gi - 1]) if gi else ""
        nxt = el.chunk_text(groups[gi + 1]) if gi + 1 < len(groups) else ""
        key = cache_key(plan.model, voice, plan.voice_settings, text, prev, nxt)
        pcm_file, meta_file = plan.cache_dir / f"{key}.pcm", plan.cache_dir / f"{key}.json"
        try:
            meta = json.loads(meta_file.read_text())
            pcm = pcm_file.read_bytes()
            if hashlib.sha256(pcm).hexdigest() != meta.get("sha256"):
                raise ValueError("cache entry damaged")
        except (OSError, ValueError):
            cost = len(text) * plan.credits_per_character
            row = plan.reserve(key, len(text), cost)  # committed before anything is sent
            try:
                speech = plan.client.speech(voice, text, model_id=plan.model,
                                            voice_settings=plan.voice_settings,
                                            previous_text=prev, next_text=nxt)  # fmt: skip
                pcm = speech.pcm
                meta = {"sha256": hashlib.sha256(pcm).hexdigest(), "starts": speech.char_start_s,
                        "ends": speech.char_end_s, "request_id": speech.request_id}  # fmt: skip
                for path, data in ((pcm_file, pcm), (meta_file, json.dumps(meta).encode())):
                    tmp = path.with_name(path.name + ".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, path)
            except el.ElevenLabsError as exc:
                if exc.code == "key_removed" or (
                    exc.status is not None and 400 <= exc.status < 500
                ):
                    plan.release(row)  # refused up front (bad key, quota, 429s used up)
                    raise
                plan.settle(row, "uncertain")
                raise UncertainCharge(
                    f"ElevenLabs may have charged {cost:.0f} credits for a request whose audio "
                    f"never arrived ({exc.message}). It is not sent again automatically; check "
                    "your ElevenLabs usage, then Retry (it still counts against the cap)"
                ) from exc
            except BaseException:
                plan.settle(row, "uncertain")
                raise
            plan.settle(row, "billed")
        base = pos / sr
        offset = 0
        n_samples = len(pcm) // 2
        chunk_end = n_samples / sr
        for s in group:
            first = offset + (len(s["text"]) - len(s["text"].lstrip()))
            last = offset + len(s["text"].rstrip()) - 1
            a = float(meta["starts"][first])
            b = float(meta["ends"][max(first, last)])
            a = min(max(0.0, a), chunk_end)
            b = min(max(a, b), chunk_end)
            timings.append(
                {"n": s["n"], "start_s": round(base + a, 3), "end_s": round(base + b, 3)}
            )
            offset += len(s["text"]) + 1
        out.write(pcm)
        pos += n_samples
        gap = silence(float(group[-1].get("pause_s", 0.3)), sr)
        out.write(gap)
        pos += len(gap) // 2
        step(len(group), len(text))
    # A chunk's start can only be later than the previous chunk's sentences, so times stay
    # monotone; within a chunk the service's character times are monotone.
    for i in range(1, len(timings)):
        if timings[i]["start_s"] < timings[i - 1]["start_s"]:
            timings[i]["start_s"] = timings[i - 1]["start_s"]
            timings[i]["end_s"] = max(timings[i]["end_s"], timings[i]["start_s"])
    return timings
