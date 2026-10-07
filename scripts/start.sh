#!/usr/bin/env bash
# Start Travel Disruption Radar for a live demo.
#   ./scripts/start.sh                # http://127.0.0.1:8000 with AI (key read from .env)
#   ./scripts/start.sh --no-ai        # http://127.0.0.1:8001 in rule-based fallback mode (side-by-side demo)
#   ./scripts/start.sh --port 9000    # choose a port;  --no-browser  skips opening a browser tab
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=""
NO_AI=0
OPEN_BROWSER=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-ai) NO_AI=1 ;;
    --port) PORT="${2:?--port needs a value}"; shift ;;
    --no-browser) OPEN_BROWSER=0 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done
if [ -z "$PORT" ]; then
  if [ "$NO_AI" = 1 ]; then PORT=8001; else PORT=8000; fi
fi

if [ ! -x .venv/bin/uvicorn ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ python3 not found. Install Python 3.10 or newer from https://www.python.org/downloads/"
    exit 1
  fi
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "✗ Python 3.10 or newer is required (found $(python3 --version 2>&1))."
    exit 1
  fi
  echo "→ First run: creating .venv and installing dependencies (about a minute)…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ Created .env from .env.example. Add your ANTHROPIC_API_KEY to it to enable AI."
fi

if [ "$NO_AI" = 1 ]; then
  # An empty variable in the environment wins over .env (python-dotenv doesn't override).
  export ANTHROPIC_API_KEY=""
  echo "→ AI disabled for this instance: rule-based fallback mode."
else
  if [ "${ANTHROPIC_API_KEY+set}" = set ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    unset ANTHROPIC_API_KEY
  fi
  if [ -z "${ANTHROPIC_API_KEY:-}" ] && ! grep -qE '^ANTHROPIC_API_KEY=[^[:space:]]+' .env; then
    echo "⚠  No ANTHROPIC_API_KEY in .env: the app will run in rule-based fallback mode (no AI)."
  fi
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ Port $PORT is already in use. Is the app already running? Try http://127.0.0.1:$PORT"
  echo "  Or use another port:  ./scripts/start.sh --port 8002"
  exit 1
fi

URL="http://127.0.0.1:$PORT"
if [ "$OPEN_BROWSER" = 1 ]; then
  ( for _ in $(seq 1 60); do
      if curl -fs "$URL/api/health" >/dev/null 2>&1; then
        open "$URL" >/dev/null 2>&1 || xdg-open "$URL" >/dev/null 2>&1 || true
        exit 0
      fi
      sleep 0.5
    done ) &
fi

echo "→ Travel Disruption Radar: $URL   (press Ctrl+C to stop)"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
