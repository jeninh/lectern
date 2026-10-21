"""`lectern` console entry point.

- ``lectern`` / ``lectern serve``: run the server.
- ``lectern worker --server URL``: an external alignment worker (needs ``lectern[align]``).
- ``lectern align-transcribe AUDIO...``: timed words of audio files as JSON (how
  ``tests/fixtures/magi/transcript_small.json`` was made).
- ``lectern align-bench EPUB AUDIO...``: run the whole alignment offline and print timing,
  coverage, memory, and hardware (docs/alignment.md, "Benchmark").
- ``lectern backup [--list]``: back up now with the settings saved in Admin, or list backups.
- ``lectern restore [SNAPSHOT]``: restore a backup with the server stopped (docs/backups.md).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import resource
import signal
import sys
import threading
import time
from pathlib import Path


def serve() -> None:
    import uvicorn

    from lectern.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "lectern.main:app_factory",
        factory=True,
        host=settings.host,
        port=settings.port,
        # request.client stays the socket peer; lectern.security.proxy applies forwarding
        # headers itself, and only for peers listed in LECTERN_TRUSTED_PROXIES.
        proxy_headers=False,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


def _transcriber_args(p: argparse.ArgumentParser) -> None:
    from lectern.align.transcribe import DEFAULT_MODEL, DEFAULT_OVERLAP_S, DEFAULT_WINDOW_S

    p.add_argument("--model", default=os.environ.get("LECTERN_ALIGN_MODEL", DEFAULT_MODEL))
    p.add_argument("--device", default=os.environ.get("LECTERN_ALIGN_DEVICE", "cpu"))
    p.add_argument("--compute-type", default=os.environ.get("LECTERN_ALIGN_COMPUTE_TYPE", "int8"))
    p.add_argument(
        "--threads",
        type=int,
        default=int(os.environ.get("LECTERN_ALIGN_THREADS", "0")),
        help="CPU threads for CTranslate2 (0 = its default)",
    )
    p.add_argument(
        "--window",
        type=float,
        default=float(os.environ.get("LECTERN_ALIGN_WINDOW_SECONDS", DEFAULT_WINDOW_S)),
        help="seconds of audio transcribed at a time; memory grows with this, not the book",
    )
    p.add_argument(
        "--overlap",
        type=float,
        default=float(os.environ.get("LECTERN_ALIGN_OVERLAP_SECONDS", DEFAULT_OVERLAP_S)),
        help="seconds shared by consecutive windows (at most a third of --window)",
    )
    p.add_argument(
        "--models-dir",
        type=Path,
        default=None,
        help="where models are downloaded (default: the Hugging Face cache)",
    )


def _transcriber(args: argparse.Namespace):
    from lectern.align.transcribe import Transcriber, check_window, transcription_available

    if not transcription_available():
        sys.exit("faster-whisper is not installed: pip install 'lectern[align]'")
    try:
        check_window(args.window, args.overlap)
    except ValueError as exc:
        sys.exit(f"--window/--overlap: {exc}")
    return Transcriber(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        threads=args.threads,
        download_root=args.models_dir,
        window_s=args.window,
        overlap_s=args.overlap,
    )


def _read_key(args: argparse.Namespace) -> str:
    if args.key_file:
        key = args.key_file.read_text().strip()
    else:
        key = os.environ.get("LECTERN_WORKER_KEY", "").strip()
    if not key.startswith("lw_"):
        sys.exit(
            "a worker credential is required: LECTERN_WORKER_KEY=lw_… or --key-file "
            "(create one under Admin → Workers, POST /api/worker-credentials)"
        )
    return key


def worker(args: argparse.Namespace) -> None:
    from lectern.align.worker import RemoteWorker

    server = args.server.rstrip("/")
    if not server.startswith("https://") and not args.allow_http:
        sys.exit(
            "refusing to send the worker credential over plain http; use an https:// URL "
            "(or --allow-http on a trusted network)"
        )
    key = _read_key(args)
    stop = threading.Event()
    w = RemoteWorker(
        server,
        key,
        transcriber=_transcriber(args),
        name=args.name,
        lease_s=args.lease,
        poll_s=args.poll,
        workdir=args.workdir,
        stop=stop,
    )

    def _on_signal(signum, _frame) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        logging.getLogger("lectern.align.worker").warning(
            "stopping after handing back the current job (signal again to force)"
        )
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        if args.once:
            w.run_once()
        else:
            w.run_forever()
    except PermissionError as exc:
        sys.exit(str(exc))


def align_transcribe(args: argparse.Namespace) -> None:
    from lectern.align.tracks import probe
    from lectern.align.transcribe import TrackAudio, transcribe_tracks

    tr = _transcriber(args)
    t0 = time.monotonic()
    tr.load()
    load_s = time.monotonic() - t0
    tracks, offset = [], 0.0
    for path in args.audio:
        duration = probe(path)[0]
        tracks.append(TrackAudio(path, offset, duration))
        offset += duration
    t1 = time.monotonic()
    words, language = transcribe_tracks(tr, tracks, args.language)
    transcribe_s = time.monotonic() - t1
    out = _words_json(
        f"faster-whisper {args.model} {args.compute_type} {args.device}, beam "
        f"{tr.beam_size}, vad_filter, word_timestamps; " + ", ".join(p.name for p in args.audio),
        language,
        words,
    )
    stats = {
        "model": tr.label,
        "audio_s": round(offset, 3),
        "load_s": round(load_s, 2),
        "transcribe_s": round(transcribe_s, 2),
        "realtime_factor": round(offset / transcribe_s, 3) if transcribe_s else None,
        "words": len(words),
        "peak_rss_mb": _peak_rss_mb(),
    }
    text = json.dumps(out, indent=0, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + "\n")
    else:
        print(text)
    print(json.dumps(stats), file=sys.stderr)


def align_bench(args: argparse.Namespace) -> None:
    from lectern.align.pipeline import TrackInput, align_work
    from lectern.align.tracks import probe
    from lectern.align.transcribe import Word
    from lectern.align.worker import transcriber_fn
    from lectern.ingest.hashing import sha256_file

    tracks, offset = [], 0.0
    for i, path in enumerate(args.audio):
        duration = probe(path)[0]
        tracks.append(TrackInput(i, path, sha256_file(path), duration, round(offset, 3)))
        offset += duration
    load_s = None
    if args.transcript:
        recorded = json.loads(args.transcript.read_text())
        words = [Word(w[0], float(w[1]), float(w[2]), float(w[3])) for w in recorded["words"]]

        def transcribe(_tracks, _lang, _progress):
            return words, recorded.get("language")

        label = f"recorded:{args.transcript.name}"
    else:
        tr = _transcriber(args)
        t0 = time.monotonic()
        tr.load()
        load_s = round(time.monotonic() - t0, 2)
        transcribe, label = transcriber_fn(tr), tr.label
    if args.words_out:
        inner = transcribe

        def transcribe(tracks, language, progress):
            words, detected = inner(tracks, language, progress)
            args.words_out.write_text(json.dumps(_words_json(label, detected, words)) + "\n")
            return words, detected

    out = align_work(
        work_id=1,  # offline: no server row; the map is still schema-valid
        epub_path=args.epub,
        epub_sha256=sha256_file(args.epub),
        tracks=tracks,
        model_label=label,
        transcribe=transcribe,
        language=args.language,
    )
    if args.output:
        args.output.write_text(out.sentence_map.model_dump_json(indent=1) + "\n")
    report = {
        "stats": {**out.stats, "model_load_s": load_s, "peak_rss_mb": _peak_rss_mb()},
        "config": {"window_s": args.window, "overlap_s": args.overlap, "model": args.model},
        "hardware": _hardware(),
    }
    print(json.dumps(report, indent=2))


def backup(args: argparse.Namespace) -> None:
    from lectern import backups
    from lectern.config import get_settings
    from lectern.db import init_engine, session_factory

    settings = get_settings()
    if args.list:
        if not (settings.backup_dir / "restic" / "config").is_file():
            sys.exit(f"no backups yet in {settings.backup_dir}")
        try:
            for snap in backups.restic_snapshots(settings):
                print(f"{snap['short_id']}\t{snap['time']}")
        except backups.BackupError as exc:
            sys.exit(str(exc))
        return
    if not settings.db_path.is_file():
        sys.exit(f"no database at {settings.db_path}")
    init_engine(settings)
    status = backups.run_and_record(session_factory(), settings, reason="cli")
    if status.get("skipped"):
        sys.exit("another backup is already running; nothing to do")
    if status.get("last_result") != "ok":
        sys.exit(f"backup failed: {status.get('last_error')}")
    print(f"backup written: {status['last_location']} ({status.get('last_size_bytes')} bytes)")
    if status.get("last_skipped_files"):
        print(f"warning: {status['last_skipped_files']} unreadable file(s) were left out")


def restore(args: argparse.Namespace) -> None:
    from lectern import backups
    from lectern.config import get_settings

    settings = get_settings()
    if args.restic_password_file:
        settings = settings.model_copy(
            update={"backup_restic_password_file": args.restic_password_file}
        )
    try:
        backups.restore(settings, args.source, restore_library=not args.no_library)
    except backups.BackupError as exc:
        sys.exit(f"restore failed: {exc}")
    print("done. Start Lectern again; it migrates the database on start if needed.")


def _words_json(source: str, language: str | None, words) -> dict:
    """The recorded-transcript format of ``tests/fixtures/magi/transcript_small.json``."""
    return {
        "source": source,
        "language": language,
        "words": [
            [w.text, round(w.start, 3), round(w.end, 3), round(w.probability, 3)] for w in words
        ],
    }


def _peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _hardware() -> dict:
    cpu = platform.processor() or platform.machine()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_gb = None
    try:
        pages = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mem_gb = round(pages / 2**30, 1)
    except (ValueError, OSError):
        pass
    return {
        "cpu": cpu,
        "machine": platform.machine(),
        "cpus_available": len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count(),
        "memory_gb": mem_gb,
        "python": platform.python_version(),
        "os": platform.platform(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lectern", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (the default)")

    w = sub.add_parser("worker", help="run an external alignment worker")
    w.add_argument("--server", required=True, help="Lectern's public URL, https://…")
    w.add_argument("--key-file", type=Path, help="file holding the lw_… credential")
    w.add_argument("--name", default=platform.node() or "worker", help="shown in the job list")
    w.add_argument("--lease", type=int, default=600, help="lease seconds (60-3600)")
    w.add_argument(
        "--poll", type=float, default=15.0, help="seconds between empty claims (at most 240)"
    )
    w.add_argument("--workdir", type=Path, default=None, help="scratch space for inputs")
    w.add_argument("--once", action="store_true", help="run at most one job and exit")
    w.add_argument("--allow-http", action="store_true", help="permit http:// (trusted LAN only)")
    _transcriber_args(w)

    t = sub.add_parser("align-transcribe", help="transcribe audio to timed words (JSON)")
    t.add_argument("audio", type=Path, nargs="+")
    t.add_argument("-o", "--output", type=Path)
    t.add_argument("--language", default=None)
    _transcriber_args(t)

    b = sub.add_parser("align-bench", help="align an EPUB with audio offline and report stats")
    b.add_argument("epub", type=Path)
    b.add_argument("audio", type=Path, nargs="+")
    b.add_argument("--transcript", type=Path, help="use recorded words instead of a model")
    b.add_argument("-o", "--output", type=Path, help="write the sentence map here")
    b.add_argument("--words-out", type=Path, help="also save the transcript (re-match later)")
    b.add_argument("--language", default=None)
    _transcriber_args(b)

    k = sub.add_parser("backup", help="back up now with the settings saved in Admin")
    k.add_argument("--list", action="store_true", help="list the snapshots")

    r = sub.add_parser(
        "restore",
        help="restore a backup (stop the server first)",
        description="Restore a backup into LECTERN_DATA_DIR and LECTERN_LIBRARY_DIR. Refuses "
        "while a server runs on the data directory. Replaced files are moved to "
        "<data>/pre-restore-<time>/ (library files too); library files are never deleted. A "
        "failure part-way through is rolled back.",
    )
    r.add_argument(
        "source",
        nargs="?",
        default="latest",
        help="snapshot id from `lectern backup --list`, or latest (the default)",
    )
    r.add_argument("--no-library", action="store_true", help="restore data only, not library files")
    r.add_argument("--restic-password-file", type=Path, help="the restic password you saved")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command in (None, "serve"):
        serve()
        return
    logging.basicConfig(
        level=os.environ.get("LECTERN_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    {
        "worker": worker,
        "align-transcribe": align_transcribe,
        "align-bench": align_bench,
        "backup": backup,
        "restore": restore,
    }[args.command](args)


if __name__ == "__main__":
    main()
