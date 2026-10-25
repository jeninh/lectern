#!/bin/sh
# Lectern worker installer (docs/mac-worker.md). Served by your Lectern server at
# /worker/install.sh, so it already knows the server's address:
#
#   curl -fsSL https://lectern.example/worker/install.sh | sh
#
# It asks for the one-time code (or reads $LECTERN_WORKER_CODE, or --code), installs uv (if
# missing), installs Lectern with the Mac extra (MLX alignment, Kokoro TTS, the menu bar) as a
# uv tool, pinned to the server's own version when the server knows it, checks for ffmpeg,
# enrolls this machine, and installs the launchd agents. Nothing runs as root. Re-running it
# upgrades in place.
set -eu

SERVER=__LECTERN_SERVER__
SOURCE=${LECTERN_WORKER_SOURCE:-__LECTERN_SOURCE__}
CODE="${LECTERN_WORKER_CODE:-}"
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

if [ -z "$CODE" ] && (: </dev/tty) 2>/dev/null; then
  # Asked for, not typed on the command line: the code stays out of the shell history.
  printf 'One-time code from Admin → Workers (lwe_…): ' >/dev/tty
  stty -echo </dev/tty 2>/dev/null || true
  read -r CODE </dev/tty || true
  stty echo </dev/tty 2>/dev/null || true
  printf '\n' >/dev/tty
fi
[ -n "$CODE" ] || die "no enrollment code: make one under Admin → Workers, then run this again"
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

case "$SOURCE" in
  git+*)
    # A private repository needs git access on this Mac; find out now, not halfway through.
    REPO=${SOURCE#git+}
    case "${REPO##*/}" in *@*) REPO=${REPO%@*} ;; esac
    command -v git >/dev/null 2>&1 || die "git is needed to install from $REPO (xcode-select --install)"
    if ! GIT_TERMINAL_PROMPT=0 git ls-remote "$REPO" HEAD >/dev/null 2>&1; then
      die "cannot read $REPO. It is private: sign in first (e.g. 'gh auth login' then
'gh auth setup-git', or an SSH key with --source git+ssh://git@github.com/OWNER/REPO), then
run this again. Behind a school proxy, pass --proxy and --ca-bundle (docs/mac-worker.md)."
    fi
    case "${SOURCE##*/}" in
      *@*) ;;
      *) say "the server does not know its own version: installing the newest code" ;;
    esac
    ;;
esac

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
set -- --server "$SERVER" --proxy "$PROXY" --no-service
[ -n "$NAME" ] && set -- "$@" --name "$NAME"
[ -n "$CA_BUNDLE" ] && set -- "$@" --ca-bundle "$CA_BUNDLE"
[ -n "$ALLOW_HTTP" ] && set -- "$@" "$ALLOW_HTTP"
LECTERN_WORKER_CODE="$CODE" lectern worker-enroll "$@"  # not in argv: ps shows argv

# After enrolling, so the downloads use the proxy and CAs saved in config.json. Models and
# voices now, with progress, instead of in the first job; every engine is tried once. A
# failure is not fatal: the worker offers only what passes (docs/mac-worker.md, Self-test).
say "preparing engines (first run downloads the alignment model and Kokoro's voices)"
lectern worker-doctor --prepare --no-restart || say "some engines are not ready: see above; the worker offers the rest"

if [ "$(uname -s)" = "Darwin" ]; then
  if [ "$LOGIN" = "no" ]; then lectern worker-service install --no-login; else lectern worker-service install; fi
fi

if [ "$(uname -s)" = "Darwin" ]; then
  say "done: the Lectern Worker menu-bar icon appears in a few seconds"
  if [ "$LOGIN" = "yes" ]; then
    launchctl kickstart "gui/$(id -u)/dev.lectern.worker.menubar" >/dev/null 2>&1 || true
  fi
else
  say "done: run 'lectern worker-agent' (or add it to systemd --user)"
fi
