#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN
status=0

run_gate() {
  local name="$1"
  local token="$2"
  local gate_status=0
  local -a command=()

  case "$token" in
    "verify-paper-environment"|"scripts/verify-paper-environment.sh")
      command=("$ROOT/scripts/verify-paper-environment.sh")
      ;;
    "verify-paper-focused"|"scripts/verify-paper-focused.sh")
      command=("$ROOT/scripts/verify-paper-focused.sh")
      ;;
    "verify-paper-gates"|"scripts/verify-paper-gates.sh")
      command=("$ROOT/scripts/verify-paper-gates.sh")
      ;;
    "full-tests")
      command=("env" "PYTHONDONTWRITEBYTECODE=1" "PYTHONPATH=src" "$PYTHON_BIN" -m unittest discover -s tests -v)
      ;;
    "git-diff-check")
      command=(git diff --check)
      ;;
    "model-unchanged")
      command=(git diff --exit-code -- models/latest_model.json)
      ;;
    "safety-pattern-live")
      command=("$PYTHON_BIN" scripts/verify-safety-patterns.py --mode live)
      ;;
    "safety-pattern-futures")
      command=("$PYTHON_BIN" scripts/verify-safety-patterns.py --mode futures)
      ;;
    "ruff-critical-lint")
      command=("$PYTHON_BIN" -m ruff check src tests --select E9,F63,F7,F82)
      ;;
    "mypy-scoped-typing")
      local -a mypy_targets=()
      read -r -a mypy_targets <<< "$VERIFY_RELEASE_MYPY_TARGETS"
      command=("$PYTHON_BIN" -m mypy "${mypy_targets[@]}")
      ;;
    "pip-audit-dry-run")
      command=("$PYTHON_BIN" -m pip_audit --dry-run --cache-dir /tmp/pip-audit-cache)
      ;;
    "pip-audit-network")
      command=("$PYTHON_BIN" -m pip_audit --cache-dir /tmp/pip-audit-cache)
      ;;
    "bandit-security-scan")
      command=("$PYTHON_BIN" -m bandit -q -ll -r src/trading_ai)
      ;;
    "coverage-gate")
      command=("$PYTHON_BIN" -m pytest --cov=src/trading_ai --cov-report=term-missing --cov-fail-under=75 -q)
      ;;
    *)
      gate_status=2
      ;;
  esac

  printf '\n==> %s\n' "$name"
  if [ "${#command[@]}" -eq 0 ]; then
    printf 'FAILED: %s (invalid command token: %s)\n' "$name" "$token"
    if [ "$status" -eq 0 ]; then
      status="$gate_status"
    fi
    return 0
  fi

  printf 'COMMAND: %s\n' "${command[*]}"
  "${command[@]}"
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
VERIFY_RELEASE_FULL_TEST_CMD="${VERIFY_RELEASE_FULL_TEST_CMD:-full-tests}"
VERIFY_RELEASE_DIFF_CMD="${VERIFY_RELEASE_DIFF_CMD:-git-diff-check}"
VERIFY_RELEASE_MODEL_CMD="${VERIFY_RELEASE_MODEL_CMD:-model-unchanged}"
VERIFY_RELEASE_LIVE_SCAN_CMD="${VERIFY_RELEASE_LIVE_SCAN_CMD:-safety-pattern-live}"
VERIFY_RELEASE_FUTURES_SCAN_CMD="${VERIFY_RELEASE_FUTURES_SCAN_CMD:-safety-pattern-futures}"
VERIFY_RELEASE_RUFF_CMD="${VERIFY_RELEASE_RUFF_CMD:-ruff-critical-lint}"
VERIFY_RELEASE_MYPY_TARGETS="${VERIFY_RELEASE_MYPY_TARGETS:-src/trading_ai/execution/paper_auto_cycle.py src/trading_ai/execution/paper_common.py src/trading_ai/execution/paper_execute_session.py src/trading_ai/execution/paper_model_alias.py src/trading_ai/execution/paper_monitor.py src/trading_ai/execution/paper_rehearsal.py src/trading_ai/execution/llm_paper_review.py src/trading_ai/execution/llm_signal_proposals.py src/trading_ai/llm/factory.py src/trading_ai/llm/local_registry.py}"
VERIFY_RELEASE_COVERAGE_CMD="${VERIFY_RELEASE_COVERAGE_CMD:-coverage-gate}"
VERIFY_RELEASE_MYPY_CMD="${VERIFY_RELEASE_MYPY_CMD:-mypy-scoped-typing}"
VERIFY_RELEASE_PIP_AUDIT_CMD="${VERIFY_RELEASE_PIP_AUDIT_CMD:-pip-audit-dry-run}"
VERIFY_RELEASE_BANDIT_CMD="${VERIFY_RELEASE_BANDIT_CMD:-bandit-security-scan}"

run_gate "paper environment" "$VERIFY_RELEASE_ENVIRONMENT_CMD"
run_gate "focused paper tests" "$VERIFY_RELEASE_FOCUSED_CMD"
run_gate "paper gates" "$VERIFY_RELEASE_PAPER_GATES_CMD"
run_gate "full unittest suite" "$VERIFY_RELEASE_FULL_TEST_CMD"
run_gate "git diff whitespace check" "$VERIFY_RELEASE_DIFF_CMD"
run_gate "latest model unchanged" "$VERIFY_RELEASE_MODEL_CMD"
run_gate "live authorization safety scan" "$VERIFY_RELEASE_LIVE_SCAN_CMD"
run_gate "futures execution parser scan" "$VERIFY_RELEASE_FUTURES_SCAN_CMD"
run_gate "ruff critical lint" "$VERIFY_RELEASE_RUFF_CMD"
run_gate "coverage gate" "$VERIFY_RELEASE_COVERAGE_CMD"
run_gate "mypy scoped typing" "$VERIFY_RELEASE_MYPY_CMD"
run_gate "pip dependency audit" "$VERIFY_RELEASE_PIP_AUDIT_CMD"
run_gate "bandit security scan" "$VERIFY_RELEASE_BANDIT_CMD"

exit "$status"
