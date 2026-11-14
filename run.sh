#!/usr/bin/env bash
# Start FBD Studio. Works from any directory; extra options are passed through:
#   ./run.sh                        opens http://127.0.0.1:8765/ in your browser
#   ./run.sh --port 9000 --no-browser
#   ./run.sh --workspace ~/my-plc-projects
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VPY="$DIR/.venv/bin/python"

if ! [ -x "$VPY" ] || ! "$VPY" -c "import fbdstudio, fastapi, uvicorn" 2>/dev/null; then
  echo "FBD Studio is not set up yet (or the setup is incomplete). Running setup first..."
  "$DIR/setup.sh"
fi

exec "$VPY" -m fbdstudio "$@"
