#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m unittest \
  tests.test_adaptive_training_cycle \
  tests.test_model_challenger_report \
  tests.test_paper_challenger_shadow_plan \
  tests.test_paper_signal_arbitration \
  tests.test_llm_context_pack \
  tests.test_paper_ops_rehearsal \
  -v
