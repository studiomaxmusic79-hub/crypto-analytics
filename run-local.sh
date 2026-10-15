#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
echo 'Open http://127.0.0.1:8000 in your browser. Ctrl+C stops the server.'
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
