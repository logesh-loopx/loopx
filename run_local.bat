@echo off
setlocal
title LOOPX
cd /d "%~dp0"
echo.
echo  ===== LOOPX - starting on this computer =====
echo.

if not exist "backend\app\main.py" goto notextracted

rem ---- find Python 3.11 or newer (the "py" launcher first, then "python") ----
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto nopython

if exist ".venv\Scripts\python.exe" goto install
echo  [1/4] Creating a private Python environment (.venv)...
%PY% -m venv .venv
if errorlevel 1 goto fail

:install
echo  [2/4] Installing the packages LOOPX needs. The first time takes 1 to 3 minutes...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto fail

echo  [3/4] Preparing the settings file (.env)...
".venv\Scripts\python.exe" scripts\init_env.py
if errorlevel 1 goto fail

echo  [4/4] Starting LOOPX. Your browser opens http://localhost:8000 in a few seconds.
echo        Keep this window open while you use LOOPX. Close it (or press Ctrl+C) to stop.
echo.
start "" /min cmd /c "timeout /t 8 /nobreak >nul & start "" http://localhost:8000"
cd backend
"..\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo  LOOPX has stopped. If you see an error above, take a screenshot and send it to Claude.
pause
exit /b 0

:notextracted
echo  This file must be run from the unzipped LOOPX folder.
echo  Right-click loopx.zip, choose "Extract All...", open the extracted loopx folder,
echo  and double-click run_local.bat there.
pause
exit /b 1

:nopython
echo  Python 3.11 or newer was not found on this computer.
echo.
echo  1. Go to https://www.python.org/downloads/ and download Python 3.12 or newer.
echo  2. Run the installer and TICK "Add python.exe to PATH" on the first screen.
echo  3. Close this window and double-click run_local.bat again.
echo.
pause
exit /b 1

:fail
echo.
echo  Something went wrong in the step above. Take a screenshot of this window and send it to Claude.
pause
exit /b 1
