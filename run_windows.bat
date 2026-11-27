@echo off
setlocal
cd /d "%~dp0"
set "PORT=8010"
set "URL=http://127.0.0.1:%PORT%/"
title Civ VI City Studio - CITY SIMULATOR v0.5.1
echo =============================================
echo Civ VI City Studio - CITY SIMULATOR v0.5.1
echo Project folder: %CD%
echo =============================================

rem Already running on this port? Just open the browser.
curl -s -m 2 "%URL%health" 2>nul | find "0.5.1" >nul
if not errorlevel 1 (
  echo Server already running - opening %URL%
  start "" "%URL%"
  exit /b 0
)

rem First run: create the virtual environment.
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -m venv .venv || python -m venv .venv
)

rem Install packages only when requirements.txt changed since the last install.
fc /b requirements.txt .venv\requirements.installed >nul 2>&1
if errorlevel 1 (
  echo Installing packages...
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt && copy /y requirements.txt .venv\requirements.installed >nul
)

rem Open the browser about 3 seconds after the server starts.
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start %URL%"
echo Starting server at %URL%  (close this window to stop)
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
pause
