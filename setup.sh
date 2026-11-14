#!/usr/bin/env bash
# FBD Studio - one-time setup (Linux / macOS)
#
#   ./setup.sh                 create/repair .venv and install dependencies
#   ./setup.sh --platformio    also install PlatformIO (needed for Build/Upload to real boards)
#   ./setup.sh --recreate      delete the existing .venv and start fresh
#   ./setup.sh --no-dev        skip test tools (pytest, httpx)
#
# Safe to run again at any time: it only does what is missing.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
VENV=".venv"
MIN_PY_MINOR=10

WITH_PIO=0
RECREATE=0
EXTRAS="dev"
for arg in "$@"; do
  case "$arg" in
    --platformio) WITH_PIO=1 ;;
    --recreate) RECREATE=1 ;;
    --no-dev) EXTRAS="" ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi
step() { echo "${B}==>${N} $*"; }
# run pip quietly; show its full output only if it fails
pip_quiet() {
  local log; log="$(mktemp)"
  if ! "$VPY" -m pip install --disable-pip-version-check "$@" >"$log" 2>&1; then
    cat "$log"; rm -f "$log"
    fail "pip install $* failed (see output above)"
  fi
  rm -f "$log"
}
ok()   { echo "    ${G}✓${N} $*"; }
warn() { echo "    ${Y}!${N} $*"; }
fail() { echo "    ${R}✗${N} $*"; exit 1; }

# --------------------------------------------------------------- 1. Python
step "Looking for Python 3.${MIN_PY_MINOR}+"
PY=""
for cand in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY_MINOR) else 1)" 2>/dev/null; then
    PY="$(command -v "$cand")"
    break
  fi
done
[ -n "$PY" ] || fail "Python 3.${MIN_PY_MINOR} or newer not found. Install it (e.g. 'sudo apt install python3') and run this script again."
ok "$("$PY" --version) at $PY"

# --------------------------------------------------------------- 2. virtual environment
step "Virtual environment ($VENV)"
if [ "$RECREATE" = 1 ] && [ -d "$VENV" ]; then
  rm -rf "$VENV"
  ok "removed old $VENV"
fi
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import sys" 2>/dev/null; then
  ok "already exists ($("$VENV/bin/python" --version))"
else
  [ -d "$VENV" ] && { warn "existing $VENV is broken, recreating it"; rm -rf "$VENV"; }
  if ! "$PY" -m venv "$VENV" 2>/tmp/fbd-venv-err.$$; then
    cat /tmp/fbd-venv-err.$$; rm -f /tmp/fbd-venv-err.$$
    ver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    fail "Could not create the venv. On Debian/Ubuntu install the venv module: sudo apt install python${ver}-venv"
  fi
  rm -f /tmp/fbd-venv-err.$$
  ok "created"
fi
VPY="$VENV/bin/python"

# --------------------------------------------------------------- 3. dependencies
step "Installing FBD Studio and its dependencies"
pip_quiet --upgrade pip
TARGET="."
[ -n "$EXTRAS" ] && TARGET=".[${EXTRAS}]"
pip_quiet -e "$TARGET"
"$VPY" -c "import fastapi, uvicorn, pydantic, yaml, serial" || fail "dependency check failed"
ok "installed ($( "$VPY" -c 'import fbdstudio; print("fbdstudio", fbdstudio.__version__)'))"

if [ "$WITH_PIO" = 1 ]; then
  step "Installing PlatformIO"
  pip_quiet "platformio>=6.1"
  ok "$("$VENV/bin/pio" --version)"
  echo "      (the first build for a board downloads its toolchain, several hundred MB)"
fi

# --------------------------------------------------------------- 4. optional tools
step "Checking optional tools"
if command -v g++ >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1; then
  ok "C++ compiler found (simulator will work)"
else
  warn "no g++/clang++: the simulator needs one (Debian/Ubuntu: sudo apt install g++)"
fi

if [ -x "$VENV/bin/pio" ] || command -v pio >/dev/null 2>&1 || [ -x "$HOME/.platformio/penv/bin/pio" ]; then
  ok "PlatformIO found (Build/Upload will work)"
else
  warn "PlatformIO not installed: Build/Upload need it. Run './setup.sh --platformio' when you want it."
fi

if [ "$(uname -s)" = "Linux" ]; then
  if id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qxE 'dialout|uucp'; then
    ok "user '$USER' may use serial ports"
  else
    warn "user '$USER' is not in the 'dialout' group, so uploading to a board may fail with 'permission denied'."
    echo "      Fix: sudo usermod -aG dialout $USER   (then log out and back in)"
  fi
fi

chmod +x run.sh 2>/dev/null || true
echo
echo "${G}${B}Setup complete.${N} Start FBD Studio with:  ${B}./run.sh${N}"
