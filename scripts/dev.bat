@echo off
REM Windows: run the app with Python only (no Node.js needed).
REM The backend serves the API AND the pre-built dashboard at http://localhost:8000
REM Reads settings from .env in the project root (scripts\live.bat forces live mode).
cd /d "%~dp0\.."

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" not found. Install Python 3.11+ from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during installation.
  goto :error
)
if not exist .venv (
  echo Creating Python virtual environment...
  py -m venv .venv || goto :error
)
echo Installing/updating Python packages (first time takes a few minutes)...
.venv\Scripts\python -m pip install -q -r backend\requirements.txt || goto :error

echo.
echo  Dashboard: http://localhost:8000   (opens in your browser in a few seconds)
echo  Keep this window open - live data is collected while it runs. Ctrl+C to stop.
echo.
start "" cmd /c "timeout /t 8 >nul & start http://localhost:8000"
cd backend
..\.venv\Scripts\python -m uvicorn app.main:app --port 8000
goto :eof

:error
echo.
echo Something failed above.
pause
