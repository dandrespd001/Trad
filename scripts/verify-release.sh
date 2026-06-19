#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
status=0

run_gate() {
  local name="$1"
  local command="$2"
  local gate_status=0

  printf '\n==> %s\n' "$name"
  printf '%s\n' "$command"
  bash -lc "$command"
  gate_status=$?
  if [ "$gate_status" -eq 0 ]; then
    printf 'PASS: %s\n' "$name"
  else
    printf 'FAILED: %s (exit %s)\n' "$name" "$gate_status"
    if [ "$status" -eq 0 ]; then
      status="$gate_status"
    fi
  fi
}

VERIFY_RELEASE_ENVIRONMENT_CMD="${VERIFY_RELEASE_ENVIRONMENT_CMD:-scripts/verify-paper-environment.sh}"
VERIFY_RELEASE_FOCUSED_CMD="${VERIFY_RELEASE_FOCUSED_CMD:-scripts/verify-paper-focused.sh}"
VERIFY_RELEASE_PAPER_GATES_CMD="${VERIFY_RELEASE_PAPER_GATES_CMD:-scripts/verify-paper-gates.sh}"
VERIFY_RELEASE_FULL_TEST_CMD="${VERIFY_RELEASE_FULL_TEST_CMD:-PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $PYTHON_BIN -m unittest discover -s tests -v}"
VERIFY_RELEASE_DIFF_CMD="${VERIFY_RELEASE_DIFF_CMD:-git diff --check}"
VERIFY_RELEASE_MODEL_CMD="${VERIFY_RELEASE_MODEL_CMD:-git diff --exit-code -- models/latest_model.json}"
VERIFY_RELEASE_LIVE_SCAN_CMD="${VERIFY_RELEASE_LIVE_SCAN_CMD:-$PYTHON_BIN scripts/verify-safety-patterns.py --mode live}"
VERIFY_RELEASE_FUTURES_SCAN_CMD="${VERIFY_RELEASE_FUTURES_SCAN_CMD:-$PYTHON_BIN scripts/verify-safety-patterns.py --mode futures}"
VERIFY_RELEASE_RUFF_CMD="${VERIFY_RELEASE_RUFF_CMD:-$PYTHON_BIN -m ruff check src tests --select E9,F63,F7,F82}"
VERIFY_RELEASE_MYPY_TARGETS="${VERIFY_RELEASE_MYPY_TARGETS:-src/trading_ai/execution/paper_auto_cycle.py src/trading_ai/execution/paper_common.py src/trading_ai/execution/paper_execute_session.py src/trading_ai/execution/paper_model_alias.py src/trading_ai/execution/paper_monitor.py src/trading_ai/execution/paper_rehearsal.py src/trading_ai/execution/llm_paper_review.py src/trading_ai/execution/llm_signal_proposals.py src/trading_ai/llm/factory.py src/trading_ai/llm/local_registry.py}"
VERIFY_RELEASE_MYPY_CMD="${VERIFY_RELEASE_MYPY_CMD:-$PYTHON_BIN -m mypy $VERIFY_RELEASE_MYPY_TARGETS}"
VERIFY_RELEASE_PIP_AUDIT_CMD="${VERIFY_RELEASE_PIP_AUDIT_CMD:-$PYTHON_BIN -m pip_audit --dry-run --cache-dir /tmp/pip-audit-cache}"
VERIFY_RELEASE_BANDIT_CMD="${VERIFY_RELEASE_BANDIT_CMD:-$PYTHON_BIN -m bandit -q -ll -r src/trading_ai}"

run_gate "paper environment" "$VERIFY_RELEASE_ENVIRONMENT_CMD"
run_gate "focused paper tests" "$VERIFY_RELEASE_FOCUSED_CMD"
run_gate "paper gates" "$VERIFY_RELEASE_PAPER_GATES_CMD"
run_gate "full unittest suite" "$VERIFY_RELEASE_FULL_TEST_CMD"
run_gate "git diff whitespace check" "$VERIFY_RELEASE_DIFF_CMD"
run_gate "latest model unchanged" "$VERIFY_RELEASE_MODEL_CMD"
run_gate "live authorization safety scan" "$VERIFY_RELEASE_LIVE_SCAN_CMD"
run_gate "futures execution parser scan" "$VERIFY_RELEASE_FUTURES_SCAN_CMD"
run_gate "ruff static lint" "$VERIFY_RELEASE_RUFF_CMD"
run_gate "mypy static typing" "$VERIFY_RELEASE_MYPY_CMD"
run_gate "pip dependency audit" "$VERIFY_RELEASE_PIP_AUDIT_CMD"
run_gate "bandit security scan" "$VERIFY_RELEASE_BANDIT_CMD"

exit "$status"
