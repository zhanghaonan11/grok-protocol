#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${XAI_WEBUI_HOST:-127.0.0.1}"
PORT="${XAI_WEBUI_PORT:-33844}"

# Prefer project conda python so mode2/curl_cffi deps stay consistent.
if [[ -x "/home/scv/miniconda3/bin/python3" ]]; then
  PY="/home/scv/miniconda3/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "找不到 python3" >&2
  exit 1
fi

if ! "$PY" -c 'import curl_cffi' >/dev/null 2>&1; then
  echo "当前 Python 缺少 curl_cffi: $PY" >&2
  echo "请执行: $PY -m pip install -r requirements.txt" >&2
  exit 1
fi

exec "$PY" webui_app.py --host "$HOST" --port "$PORT" "$@"
