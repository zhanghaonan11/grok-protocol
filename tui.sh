#!/usr/bin/env bash
# 无浏览器 HTTP 协议全屏 TUI 的启动入口。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="python"
    else
        printf '未找到 Python 3。请设置 PYTHON_BIN，或先安装 Python。\n' >&2
        exit 1
    fi
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/http_tui.py" "$@"
