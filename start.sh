#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "${1:-}" in
  "") ;;
  --demo) export SKILLSCOPE_DATA_DIR="$PWD/data/demo" ;;
  *) echo "Usage: ./start.sh [--demo]" >&2; exit 2 ;;
esac
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then uv venv --python 3.11 .venv; else python3 -m venv .venv; fi
fi
if ! .venv/bin/python -c 'import fastapi,uvicorn,httpx,bs4,pytest' >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then uv pip install --python .venv/bin/python -r requirements-lock.txt; else .venv/bin/python -m pip install -r requirements-lock.txt; fi
fi
if [ ! -d frontend/node_modules ]; then (cd frontend && npm ci); fi
(cd frontend && npm run build)
if [ "${1:-}" = "--demo" ]; then .venv/bin/python scripts/load_demo.py; fi
printf '\nSkillScope: http://127.0.0.1:%s\n' "${PORT:-8770}"
exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port "${PORT:-8770}"
