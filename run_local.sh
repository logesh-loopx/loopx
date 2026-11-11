#!/usr/bin/env bash
# Run LOOPX on your computer without Docker (macOS / Linux). Needs Python 3.11+.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -r requirements.txt
python3 scripts/init_env.py
cd backend
echo "LOOPX is starting at http://localhost:8000  (Ctrl+C to stop)"
exec uvicorn app.main:app --host 127.0.0.1 --port 8000
