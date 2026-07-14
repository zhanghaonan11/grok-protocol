#!/usr/bin/env bash
# 训练器 CPU 监控 TUI 启动入口
# 用法:
#   ./cpu_tui.sh
#   ./cpu_tui.sh --interval 0.5
#   ./cpu_tui.sh --once
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  :
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  PYTHON_BIN="python"
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/trainer_cpu_tui.py" "$@"
