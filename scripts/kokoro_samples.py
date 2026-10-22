"""Render the built-in Kokoro voice samples (docs/tts.md#voice-samples).

One short clip per voice in ``lectern.tts.engines.KOKORO_VOICES``: the same sentence for every
voice of a language, at speed 1.0, AAC in M4A (plays everywhere, iOS included). Clips and
``manifest.json`` go to ``src/lectern/static/voice-samples/kokoro/``; the server lists them in
the Make an audiobook voice picker. Run it again when the voice list changes:

    uv run --with "kokoro>=0.9" --with "misaki[en,ja,zh]" python scripts/kokoro_samples.py

Needs ffmpeg, and espeak-ng for Spanish, French, Hindi, Italian, and Portuguese. Kokoro
downloads its weights from Hugging Face on first use.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lectern.tts.engines import KOKORO_SAMPLE_RATE, KOKORO_VOICES, kokoro_lang  # noqa: E402

OUT = ROOT / "src" / "lectern" / "static" / "voice-samples" / "kokoro"
KBPS = 40
REPO = "hexgrad/Kokoro-82M"

# One sentence per Kokoro language code (the voice's first letter).
TEXT = {
    "a": "Once upon a time, in a quiet town by the sea, there lived a curious reader.",
    "b": "Once upon a time, in a quiet town by the sea, there lived a curious reader.",
    "e": "Érase una vez, en un pueblo tranquilo junto al mar, una lectora muy curiosa.",
    "f": "Il était une fois, dans un village tranquille au bord de la mer, une lectrice curieuse.",
    "h": "एक समय की बात है, समुद्र के किनारे एक शांत गाँव में एक जिज्ञासु पाठक रहता था।",
    "i": "C'era una volta, in un paese tranquillo vicino al mare, una lettrice molto curiosa.",
    "j": "むかしむかし、海のそばの静かな町に、本が大好きな人が住んでいました。",
    "p": "Era uma vez, numa cidade tranquila à beira-mar, uma leitora muito curiosa.",
    "z": "很久以前，在海边一个安静的小镇上，住着一位爱读书的人。",
}


def main() -> None:
    import numpy as np
    from kokoro import KModel, KPipeline

    OUT.mkdir(parents=True, exist_ok=True)
    model = KModel(repo_id=REPO).eval()  # one copy of the weights for every language
    pipe, manifest = None, {"speed": 1.0, "codec": "aac", "voices": {}}
    for voice in KOKORO_VOICES:
        lang = voice[0]
        text = TEXT[lang]
        if pipe is None or pipe.lang_code != lang:
            pipe = KPipeline(lang_code=lang, repo_id=REPO, model=model)
        chunks = [np.asarray(r.audio, dtype=np.float32) for r in pipe(text, voice=voice, speed=1.0)]
        audio = np.concatenate(chunks)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        dest = OUT / f"{voice}.m4a"
        subprocess.run(  # noqa: S603 - a fixed ffmpeg command line
            [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le",
             "-ar", str(KOKORO_SAMPLE_RATE), "-ac", "1", "-i", "-", "-c:a", "aac",
             "-b:a", f"{KBPS}k", "-map_metadata", "-1", "-fflags", "+bitexact", "-movflags", "+faststart", str(dest)],
            input=pcm, check=True,
        )  # fmt: skip
        manifest["voices"][voice] = {
            "file": dest.name,
            "language": kokoro_lang(voice),
            "text": text,
            "duration_s": round(len(audio) / KOKORO_SAMPLE_RATE, 2),
            "bytes": dest.stat().st_size,
        }
        print(f"{voice}: {manifest['voices'][voice]['duration_s']} s, {dest.stat().st_size} bytes")
    for stale in OUT.glob("*.m4a"):  # voices dropped from KOKORO_VOICES stop shipping
        if stale.stem not in manifest["voices"]:
            stale.unlink()
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    total = sum(v["bytes"] for v in manifest["voices"].values())
    print(f"{len(manifest['voices'])} samples, {total / 1024:.0f} KiB")


if __name__ == "__main__":
    main()
