#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=src

"$PYTHON_BIN" -m unittest \
  tests.test_llm_role_registry \
  tests.test_llm_training_dataset \
  tests.test_llm_supervise_labels \
  tests.test_llm_eval_suite \
  tests.test_llm_candidate_report \
  tests.test_llm_model_alias_decision \
  tests.test_llm_adaptive_review \
  tests.test_llm_training_export \
  tests.test_llm_guardrails \
  tests.test_llm_context_pack \
  tests.test_llm_paper_review \
  tests.test_llm_signal_proposals \
  tests.test_llm_local_registry \
  tests.test_llm_local_workflow \
  -v

"$PYTHON_BIN" -m trading_ai.cli llm-eval --output reports/tmp/llm_eval/latest.json
