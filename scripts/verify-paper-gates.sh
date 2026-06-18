#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

FOCUSED_CMD="${VERIFY_PAPER_FOCUSED_CMD:-scripts/verify-paper-focused.sh}"
FULL_CMD="${VERIFY_PAPER_FULL_CMD:-PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -q}"
DIFF_CMD="${VERIFY_PAPER_DIFF_CMD:-git diff --check}"
ARTIFACT_CMD="${VERIFY_PAPER_ARTIFACT_CMD:-scripts/verify-paper-artifacts.sh}"

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

run_gate "focused paper tests" "$FOCUSED_CMD"
run_gate "full unittest suite" "$FULL_CMD"
run_gate "git diff whitespace check" "$DIFF_CMD"
run_gate "artifact policy" "$ARTIFACT_CMD"

exit "$status"
