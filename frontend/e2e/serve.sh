#!/usr/bin/env bash
# Playwright's web server: a freshly seeded Lectern on :8090 serving the built app (npm run build).
set -euo pipefail
cd "$(dirname "$0")/../.."
dir="$(mktemp -d)"
uv run python frontend/e2e/seed.py "$dir"
export LECTERN_DATA_DIR="$dir/data" LECTERN_LIBRARY_DIR="$dir/library"
export LECTERN_BASE_URL="http://127.0.0.1:8090" LECTERN_INSECURE_HTTP=1 LECTERN_HOST=127.0.0.1 LECTERN_PORT=8090
export LECTERN_INGEST_ENABLED=0 LECTERN_BACKGROUND_JOBS=0 LECTERN_ALIGN_LOCAL_WORKER=0
export LECTERN_RATE_LIMIT_PER_MINUTE=100000 LECTERN_LOG_LEVEL=WARNING
exec uv run lectern serve
