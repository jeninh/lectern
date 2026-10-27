# The web app is static output, so build it natively even when targeting another platform
# (e.g. linux/arm64 under QEMU in CI); only the Python stages need the target architecture.
FROM --platform=$BUILDPLATFORM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS build
# LECTERN_EXTRAS is a space- or comma-separated list of extras, each passed to `uv sync` as
# its own --extra (e.g. LECTERN_EXTRAS="align tts" for both). Empty = the default, small image,
# which leaves alignment to `lectern worker` on another machine.
#   align: faster-whisper (~0.6 GB more image), so the server aligns itself (docs/alignment.md)
#   tts:   Piper, CPU text-to-speech for generated audiobooks (docs/tts.md)
ARG LECTERN_EXTRAS=""
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN set -f --; for e in $(echo "$LECTERN_EXTRAS" | tr ',' ' '); do set -- "$@" --extra "$e"; done; \
    uv sync --frozen --no-dev --no-install-project "$@"
COPY src ./src
COPY --from=ui /src/lectern/static/app ./src/lectern/static/app
RUN set -f --; for e in $(echo "$LECTERN_EXTRAS" | tr ',' ' '); do set -- "$@" --extra "$e"; done; \
    uv sync --frozen --no-dev "$@"

FROM python:3.12-slim-bookworm
# ffmpeg/ffprobe: audiobook metadata and chapters, data-saver renditions, sleep-timer rewind.
# restic: the encrypted, deduplicated repository behind the optional nightly backups
# (lectern.backups; off by default).
# (faster-whisper in the align image decodes through PyAV's own bundled FFmpeg.)
# fonts-noto-cjk: Chinese, Japanese, and Korean titles on generated covers (lectern.covergen;
# every other script's font ships in src/lectern/fonts). Only the serif regular face is used,
# so the sans and bold collections (~63 MB) are removed.
# tesseract-ocr: text recognition for scanned PDFs (lectern.ocr, docs/ocr.md), English data
# only; other languages are one apt package each (tesseract-ocr-fra, -deu, …).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk restic \
    tesseract-ocr tesseract-ocr-eng \
 && find /usr/share/fonts/opentype/noto -name '*.ttc' ! -name 'NotoSerifCJK-Regular.ttc' -delete \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 lectern && useradd -u 1000 -g lectern -m -s /usr/sbin/nologin lectern \
 && mkdir -p /data /library /backups && chown lectern:lectern /data /library /backups
WORKDIR /app
COPY --from=build --chown=lectern:lectern /app /app
# The commit this image was built from (docker build --build-arg LECTERN_COMMIT=$(git
# rev-parse HEAD)): /worker/install.sh pins Mac workers to the same code. Empty = unpinned.
ARG LECTERN_COMMIT=""
ENV LECTERN_WORKER_INSTALL_REF=${LECTERN_COMMIT}
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LECTERN_DATA_DIR=/data \
    LECTERN_LIBRARY_DIR=/library \
    LECTERN_PORT=8080 \
    HF_HOME=/data/huggingface
USER lectern
VOLUME ["/data", "/library"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"
CMD ["lectern"]
