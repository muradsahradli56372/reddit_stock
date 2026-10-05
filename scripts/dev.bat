@echo off
REM Windows: run without Docker. Backend on :8000 (new window), dashboard on :5173.
REM Needs Python 3.11+ ("py") and Node 18+ ("npm"). Reads settings from .env in the project root.
cd /d "%~dp0\.."

if not exist .venv (
  echo Creating Python virtual environment...
  py -m venv .venv || goto :error
)
echo Installing/updating Python packages...
.venv\Scripts\python -m pip install -q -r backend\requirements.txt || goto :error

if not exist frontend\node_modules (
  echo Installing frontend packages...
  pushd frontend
  call npm install --no-audit --no-fund || goto :error
  popd
)

start "Reddit Market Pulse - backend" cmd /k "cd /d %~dp0\..\backend && ..\.venv\Scripts\python -m uvicorn app.main:app --port 8000"
echo.
echo Backend starting in a separate window. Dashboard: http://localhost:5173
echo Close both windows (or press Ctrl+C) to stop. The backend must keep running for live data collection.
echo.
cd frontend
call npx vite --port 5173
goto :eof

:error
echo.
echo Something failed above. Check that Python 3.11+ (py) and Node.js (npm) are installed.
pause
