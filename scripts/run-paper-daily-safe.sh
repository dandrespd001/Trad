#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN

confirm_auto=0
require_clean=0
dates=()
args=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --confirm-paper-auto)
      confirm_auto=1
      args+=("$1")
      shift
      ;;
    --require-clean-state)
      require_clean=1
      args+=("$1")
      shift
      ;;
    --as-of-date|--from|--to)
      if [ "$#" -lt 2 ]; then
        echo "$1 requires an ISO date value" >&2
        exit 2
      fi
      dates+=("$1=$2")
      args+=("$1" "$2")
      shift 2
      ;;
    -h|--help)
      cat <<'USAGE'
usage: scripts/run-paper-daily-safe.sh --as-of-date YYYY-MM-DD --from YYYY-MM-DD --to YYYY-MM-DD [paper-auto-cycle args...]

Safe wrapper for paper-auto-cycle. It validates the local paper environment,
rejects relative dates, and requires --require-clean-state whenever
--confirm-paper-auto is used.
USAGE
      exit 0
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done

for item in "${dates[@]}"; do
  value="${item#*=}"
  if [[ ! "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "relative or invalid date rejected: $item" >&2
    exit 2
  fi
done

if [ "$confirm_auto" -eq 1 ] && [ "$require_clean" -ne 1 ]; then
  echo "--confirm-paper-auto requires --require-clean-state" >&2
  exit 2
fi

cd "$ROOT"
scripts/verify-paper-environment.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m trading_ai.cli paper-auto-cycle "${args[@]}"
