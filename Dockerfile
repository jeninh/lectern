FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY --from=ui /src/lectern/static/app ./src/lectern/static/app
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
# ffmpeg and faster-whisper arrive with the audiobook milestone; keep the image small for now.
RUN groupadd -g 1000 lectern && useradd -u 1000 -g lectern -m -s /usr/sbin/nologin lectern \
 && mkdir -p /data /library && chown lectern:lectern /data /library
WORKDIR /app
COPY --from=build --chown=lectern:lectern /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LECTERN_DATA_DIR=/data \
    LECTERN_LIBRARY_DIR=/library \
    LECTERN_PORT=8080
USER lectern
VOLUME ["/data", "/library"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"
CMD ["lectern"]
