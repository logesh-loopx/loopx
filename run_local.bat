@echo off
REM Run LOOPX on Windows without Docker. Needs Python 3.11+ from python.org (tick "Add to PATH").
cd /d "%~dp0"
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
python scripts\init_env.py
cd backend
echo LOOPX is starting at http://localhost:8000  (Ctrl+C to stop)
uvicorn app.main:app --host 127.0.0.1 --port 8000
