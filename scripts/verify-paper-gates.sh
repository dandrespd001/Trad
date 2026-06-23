#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN

FOCUSED_CMD="${VERIFY_PAPER_FOCUSED_CMD:-verify-paper-focused}"
FULL_CMD="${VERIFY_PAPER_FULL_CMD:-full-tests}"
DIFF_CMD="${VERIFY_PAPER_DIFF_CMD:-git-diff-check}"
ARTIFACT_CMD="${VERIFY_PAPER_ARTIFACT_CMD:-verify-paper-artifacts}"

status=0

run_gate() {
  local name="$1"
  local token="$2"
  local gate_status=0
  local -a command=()
  case "$token" in
    "verify-paper-focused"|"scripts/verify-paper-focused.sh")
      command=("$ROOT/scripts/verify-paper-focused.sh")
      ;;
    "full-tests")
      command=("env" "PYTHONDONTWRITEBYTECODE=1" "PYTHONPATH=src" "$PYTHON_BIN" -m unittest discover -s tests -q)
      ;;
    "git-diff-check")
      command=(git diff --check)
      ;;
    "verify-paper-artifacts"|"scripts/verify-paper-artifacts.sh")
      command=("$ROOT/scripts/verify-paper-artifacts.sh")
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

run_gate "focused paper tests" "$FOCUSED_CMD"
run_gate "full unittest suite" "$FULL_CMD"
run_gate "git diff whitespace check" "$DIFF_CMD"
run_gate "artifact policy" "$ARTIFACT_CMD"

exit "$status"
