#!/usr/bin/env bash
# Runs the migration up/down suite (tests/test_sqlite_legacy.py) on a real SQLite < 3.35.
#
# Local / orb only. This is deliberately NOT wired into GitHub Actions (docs/testing-legacy-sqlite.md).
# Needs Docker and network access (to pull the image and the locked dependencies). No secrets.
#
# The image is python:3.12-slim-bullseye (Debian 11, SQLite 3.34.1): the same Python minor as the
# runtime image (python:3.12-slim-bookworm, SQLite 3.40), whose SQLite is too new to test the
# pre-3.35 paths. It is pinned by digest, so a moved tag cannot change what runs. To bump it:
#   docker pull python:3.12-slim-bullseye
#   docker image inspect python:3.12-slim-bullseye --format '{{index .RepoDigests 0}}'
# Dependencies come from uv.lock (--frozen). Extra arguments go to pytest.
set -euo pipefail

IMAGE="python@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db"
UV_VERSION="0.5.31"

cd "$(dirname "$0")/.."

# The repository is mounted read-only; the virtualenv, uv cache and bytecode live in the container.
exec docker run --rm \
  --volume "$PWD:/src:ro" \
  --env LECTERN_REQUIRE_LEGACY_SQLITE=1 \
  --env UV_PROJECT_ENVIRONMENT=/tmp/venv \
  --env UV_LINK_MODE=copy \
  --env UV_PYTHON_DOWNLOADS=never \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env PYTEST_ADDOPTS="-p no:cacheprovider" \
  --workdir /src \
  "$IMAGE" \
  bash -euc '
    pip install --quiet --disable-pip-version-check "uv=='"$UV_VERSION"'"
    python -c "import sqlite3; print(\"SQLite\", sqlite3.sqlite_version)"
    uv sync --frozen --extra dev
    /tmp/venv/bin/python -m pytest -m legacy_sqlite tests/test_sqlite_legacy.py "$@"
  ' bash "$@"
