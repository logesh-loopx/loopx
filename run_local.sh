#!/usr/bin/env bash
# Run LOOPX on your computer without Docker (macOS / Linux). Needs Python 3.11+.
set -e
cd "$(dirname "$0")"
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
  echo "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi
if [ ! -d .venv ]; then echo "[1/4] Creating a private Python environment (.venv)..."; python3 -m venv .venv; fi
. .venv/bin/activate
echo "[2/4] Installing packages (first time takes 1 to 3 minutes)..."
pip install --disable-pip-version-check -q -r requirements.txt
echo "[3/4] Preparing the settings file (.env)..."
python3 scripts/init_env.py
cd backend
echo "[4/4] LOOPX is starting at http://localhost:8000  (keep this window open; Ctrl+C to stop)"
( sleep 6; (command -v open >/dev/null && open http://localhost:8000) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8000) ) >/dev/null 2>&1 &
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
