#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_DIR="${PAPER_AUTO_OUTPUT_DIR:-reports/tmp/paper_auto_cycle}"
LOCK_DIR="${PAPER_AUTO_LOCK_DIR:-$OUTPUT_DIR/locks}"

cd "$ROOT"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m trading_ai.cli paper-auto-cycle \
  --lock-dir "$LOCK_DIR" \
  "$@"
