#!/usr/bin/env bash
# Minimal smoke placeholder for later local solver API.
set -euo pipefail
HOST="${SOLVER_HOST:-127.0.0.1}"
PORT="${SOLVER_PORT:-8787}"

echo "[smoke] health => http://${HOST}:${PORT}/health"
curl -fsS "http://${HOST}:${PORT}/health" || {
  echo "[smoke] solver not running yet (expected in Phase 0)" >&2
  exit 1
}
