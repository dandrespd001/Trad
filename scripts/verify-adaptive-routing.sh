#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN

cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m unittest \
  tests.test_paper_adaptive_routing \
  tests.test_paper_signal_arbitration \
  tests.test_prepare_paper_daily \
  tests.test_paper_auto_cycle \
  tests.test_llm_context_pack \
  tests.test_llm_paper_review \
  tests.test_paper_ops_rehearsal \
  -v
