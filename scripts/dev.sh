#!/usr/bin/env bash
# Run without Docker: backend on :8000, dashboard on :5173.
# Uses DATABASE_URL from .env if set, otherwise a local SQLite file.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -d "$ROOT/.venv" ]; then
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  (cd "$ROOT/frontend" && npm install --no-audit --no-fund)
fi

(cd "$ROOT/backend" && "$ROOT/.venv/bin/uvicorn" app.main:app --port 8000) &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT
(cd "$ROOT/frontend" && npx vite --port 5173)
