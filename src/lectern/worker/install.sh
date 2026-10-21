#!/bin/sh
# Lectern worker installer (docs/mac-worker.md). Served by your Lectern server at
# /worker/install.sh, so it already knows the server's address:
#
#   curl -fsSL https://lectern.example/worker/install.sh | sh -s -- --code lwe_…
#
# It installs uv (if missing), installs Lectern with the Mac extra (MLX alignment, Kokoro TTS,
# the menu bar) as a uv tool, checks for ffmpeg, enrolls this machine with the one-time code,
# and installs the launchd agents. Nothing runs as root. Re-running it upgrades in place.
set -eu

SERVER=__LECTERN_SERVER__
SOURCE=${LECTERN_WORKER_SOURCE:-__LECTERN_SOURCE__}
CODE=""
NAME=""
PROXY="system"
CA_BUNDLE=""
LOGIN="yes"
ALLOW_HTTP=""

while [ $# -gt 0 ]; do
  case "$1" in
    --code) CODE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --proxy) PROXY="$2"; shift 2 ;;
    --ca-bundle) CA_BUNDLE="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --no-login) LOGIN="no"; shift ;;
    --allow-http) ALLOW_HTTP="--allow-http"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -n "$CODE" ] || die "pass the one-time code from Admin → Workers: --code lwe_…"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) EXTRA="mac" ;;
  Darwin-*) say "Intel Mac: no MLX; alignment runs on the CPU"; EXTRA="mac" ;;
  *) say "not a Mac: installing the CPU worker (no menu bar)"; EXTRA="align" ;;
esac

# A proxy for the downloads below, too (school mode).
if [ "$PROXY" != "system" ] && [ "$PROXY" != "none" ]; then
  export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
fi
if [ -n "$CA_BUNDLE" ]; then
  export SSL_CERT_FILE="$CA_BUNDLE" UV_NATIVE_TLS=1
fi

if ! command -v uv >/dev/null 2>&1; then
  say "installing uv (https://astral.sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
fi

say "installing lectern[$EXTRA] from $SOURCE"
uv tool install --force --python 3.12 "lectern[$EXTRA] @ $SOURCE"
PATH="$HOME/.local/bin:$PATH"

if ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    say "installing ffmpeg (Kokoro audiobooks and transcodes need it)"
    brew install ffmpeg
  else
    say "ffmpeg not found: alignment works, Kokoro and transcode stay off until it is installed"
  fi
fi

say "enrolling with $SERVER"
set -- --server "$SERVER" --code "$CODE" --proxy "$PROXY"
[ -n "$NAME" ] && set -- "$@" --name "$NAME"
[ -n "$CA_BUNDLE" ] && set -- "$@" --ca-bundle "$CA_BUNDLE"
[ "$LOGIN" = "no" ] && set -- "$@" --no-login
[ -n "$ALLOW_HTTP" ] && set -- "$@" "$ALLOW_HTTP"
lectern worker-enroll "$@"

if [ "$(uname -s)" = "Darwin" ]; then
  say "done: the Lectern Worker menu-bar icon appears in a few seconds"
  if [ "$LOGIN" = "yes" ]; then
    launchctl kickstart "gui/$(id -u)/dev.lectern.worker.menubar" >/dev/null 2>&1 || true
  fi
else
  say "done: run 'lectern worker-agent' (or add it to systemd --user)"
fi
