"""The desktop worker: the background agent, the macOS menu-bar app, and their job runners.

``lectern worker-agent`` runs :class:`lectern.align.worker.RemoteWorker` with every engine this
machine has (MLX or CPU alignment, Kokoro TTS, ffmpeg transcode), pauses it on battery or off
Wi-Fi as configured, and writes its state for ``lectern worker-menubar``. See
docs/mac-worker.md.
"""
