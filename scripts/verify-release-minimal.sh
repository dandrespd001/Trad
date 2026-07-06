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
    "verify-paper-environment-core"|"scripts/verify-paper-environment.sh --skip-research")
      command=("$ROOT/scripts/verify-paper-environment.sh" --skip-research)
      ;;
    "minimal-focused-paper-tests")
      command=(
        "env" "PYTHONDONTWRITEBYTECODE=1" "PYTHONPATH=src" "$PYTHON_BIN" -m unittest
        tests.test_alpaca_paper_connection
        tests.test_alpaca_paper_execution
        tests.test_live_readiness
        tests.test_paper_common
        tests.test_paper_gate_scripts
        -v
      )
      ;;
    "verify-paper-focused"|"scripts/verify-paper-focused.sh")
      command=("$ROOT/scripts/verify-paper-focused.sh")
      ;;
    "minimal-unittest-suite")
      command=(
        "env" "PYTHONDONTWRITEBYTECODE=1" "PYTHONPATH=src" "$PYTHON_BIN" -m unittest
        tests.test_live_readiness
        tests.test_config_loading
        tests.test_paper_gate_scripts
        -v
      )
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

VERIFY_RELEASE_MINIMAL_ENVIRONMENT_CMD="${VERIFY_RELEASE_MINIMAL_ENVIRONMENT_CMD:-verify-paper-environment-core}"
VERIFY_RELEASE_MINIMAL_FOCUSED_CMD="${VERIFY_RELEASE_MINIMAL_FOCUSED_CMD:-minimal-focused-paper-tests}"
VERIFY_RELEASE_MINIMAL_FULL_TEST_CMD="${VERIFY_RELEASE_MINIMAL_FULL_TEST_CMD:-minimal-unittest-suite}"
VERIFY_RELEASE_MINIMAL_DIFF_CMD="${VERIFY_RELEASE_MINIMAL_DIFF_CMD:-git-diff-check}"
VERIFY_RELEASE_MINIMAL_MODEL_CMD="${VERIFY_RELEASE_MINIMAL_MODEL_CMD:-model-unchanged}"
VERIFY_RELEASE_MINIMAL_LIVE_SCAN_CMD="${VERIFY_RELEASE_MINIMAL_LIVE_SCAN_CMD:-safety-pattern-live}"
VERIFY_RELEASE_MINIMAL_FUTURES_SCAN_CMD="${VERIFY_RELEASE_MINIMAL_FUTURES_SCAN_CMD:-safety-pattern-futures}"

run_gate "minimal paper environment" "$VERIFY_RELEASE_MINIMAL_ENVIRONMENT_CMD"
run_gate "minimal focused paper tests" "$VERIFY_RELEASE_MINIMAL_FOCUSED_CMD"
run_gate "minimal full unittest suite" "$VERIFY_RELEASE_MINIMAL_FULL_TEST_CMD"
run_gate "minimal git diff whitespace check" "$VERIFY_RELEASE_MINIMAL_DIFF_CMD"
run_gate "minimal latest model unchanged" "$VERIFY_RELEASE_MINIMAL_MODEL_CMD"
run_gate "minimal live authorization safety scan" "$VERIFY_RELEASE_MINIMAL_LIVE_SCAN_CMD"
run_gate "minimal futures execution parser scan" "$VERIFY_RELEASE_MINIMAL_FUTURES_SCAN_CMD"

exit "$status"
