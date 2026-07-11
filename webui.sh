#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${XAI_WEBUI_HOST:-127.0.0.1}"
PORT="${XAI_WEBUI_PORT:-33843}"
exec python3 webui_app.py --host "$HOST" --port "$PORT" "$@"
