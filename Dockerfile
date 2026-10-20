# The web app is static output, so build it natively even when targeting another platform
# (e.g. linux/arm64 under QEMU in CI); only the Python stages need the target architecture.
FROM --platform=$BUILDPLATFORM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS build
# LECTERN_EXTRAS=align adds faster-whisper (~0.6 GB more image) so the server aligns itself;
# the default image stays small and leaves alignment to `lectern worker` on another machine.
ARG LECTERN_EXTRAS=""
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project ${LECTERN_EXTRAS:+--extra $LECTERN_EXTRAS}
COPY src ./src
COPY --from=ui /src/lectern/static/app ./src/lectern/static/app
RUN uv sync --frozen --no-dev ${LECTERN_EXTRAS:+--extra $LECTERN_EXTRAS}

FROM python:3.12-slim-bookworm
# ffmpeg/ffprobe: audiobook metadata and chapters, data-saver renditions, sleep-timer rewind.
# (faster-whisper in the align image decodes through PyAV's own bundled FFmpeg.)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 lectern && useradd -u 1000 -g lectern -m -s /usr/sbin/nologin lectern \
 && mkdir -p /data /library && chown lectern:lectern /data /library
WORKDIR /app
COPY --from=build --chown=lectern:lectern /app /app
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
