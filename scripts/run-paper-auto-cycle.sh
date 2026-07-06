#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN
OUTPUT_DIR="${PAPER_AUTO_OUTPUT_DIR:-reports/tmp/paper_auto_cycle}"
LOCK_DIR="${PAPER_AUTO_LOCK_DIR:-$OUTPUT_DIR/locks}"
POSITION_WATCH="${PAPER_AUTO_POSITION_WATCH:-reports/tmp/paper_position_watch/latest.json}"
EOD_POSITION_PLAN="${PAPER_AUTO_EOD_POSITION_PLAN:-reports/tmp/paper_eod_position_plan/latest.json}"
CROSS_ASSET_SESSION_PLAN="${PAPER_AUTO_CROSS_ASSET_SESSION_PLAN:-reports/tmp/cross_asset_session_plan/latest.json}"
TELEGRAM_DISPATCH="${PAPER_AUTO_TELEGRAM_DISPATCH:-reports/tmp/telegram_control/dispatch.json}"
RISK_STATE="${PAPER_AUTO_RISK_STATE:-reports/tmp/paper_risk_state.json}"
confirm_auto=0
require_clean=0
require_operational="${PAPER_AUTO_REQUIRE_OPERATIONAL_EVIDENCE:-0}"
position_watch_provided=0
eod_position_plan_provided=0
cross_asset_session_plan_provided=0
telegram_dispatch_provided=0
risk_state_provided=0
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
    --require-operational-evidence)
      require_operational=1
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
    --position-watch)
      if [ "$#" -lt 2 ]; then
        echo "$1 requires a JSON artifact path" >&2
        exit 2
      fi
      POSITION_WATCH="$2"
      position_watch_provided=1
      args+=("$1" "$2")
      shift 2
      ;;
    --eod-position-plan)
      if [ "$#" -lt 2 ]; then
        echo "$1 requires a JSON artifact path" >&2
        exit 2
      fi
      EOD_POSITION_PLAN="$2"
      eod_position_plan_provided=1
      args+=("$1" "$2")
      shift 2
      ;;
    --cross-asset-session-plan)
      if [ "$#" -lt 2 ]; then
        echo "$1 requires a JSON artifact path" >&2
        exit 2
      fi
      CROSS_ASSET_SESSION_PLAN="$2"
      cross_asset_session_plan_provided=1
      args+=("$1" "$2")
      shift 2
      ;;
    --telegram-dispatch)
      if [ "$#" -lt 2 ]; then
        echo "$1 requires a JSON artifact path" >&2
        exit 2
      fi
      TELEGRAM_DISPATCH="$2"
      telegram_dispatch_provided=1
      args+=("$1" "$2")
      shift 2
      ;;
    --risk-state-path)
      if [ "$#" -lt 2 ]; then
        echo "$1 requires a JSON artifact path" >&2
        exit 2
      fi
      RISK_STATE="$2"
      risk_state_provided=1
      args+=("$1" "$2")
      shift 2
      ;;
    -h|--help)
      cat <<'USAGE'
usage: scripts/run-paper-auto-cycle.sh --as-of-date YYYY-MM-DD --from YYYY-MM-DD --to YYYY-MM-DD [--require-operational-evidence] [paper-auto-cycle args...]

Safe wrapper for paper-auto-cycle. It validates the local paper environment,
rejects relative dates, and requires --require-clean-state whenever
--confirm-paper-auto is used. With --require-operational-evidence, it requires
local position-watch, EOD, cross-asset, Telegram dispatch, and risk-state
artifacts before running the environment preflight or cycle.
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

case "${require_operational,,}" in
  1|true|yes)
    missing=()
    for path in "$POSITION_WATCH" "$EOD_POSITION_PLAN" "$CROSS_ASSET_SESSION_PLAN" "$TELEGRAM_DISPATCH" "$RISK_STATE"; do
      if [ ! -f "$path" ]; then
        missing+=("$path")
      fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
      echo "required operational evidence missing:" >&2
      printf '  %s\n' "${missing[@]}" >&2
        exit 2
    fi
    if [ "$position_watch_provided" -ne 1 ]; then
      args+=("--position-watch" "$POSITION_WATCH")
    fi
    if [ "$eod_position_plan_provided" -ne 1 ]; then
      args+=("--eod-position-plan" "$EOD_POSITION_PLAN")
    fi
    if [ "$cross_asset_session_plan_provided" -ne 1 ]; then
      args+=("--cross-asset-session-plan" "$CROSS_ASSET_SESSION_PLAN")
    fi
    if [ "$telegram_dispatch_provided" -ne 1 ]; then
      args+=("--telegram-dispatch" "$TELEGRAM_DISPATCH")
    fi
    if [ "$risk_state_provided" -ne 1 ]; then
      args+=("--risk-state-path" "$RISK_STATE")
    fi
    ;;
  0|false|no|"")
    ;;
  *)
    echo "invalid PAPER_AUTO_REQUIRE_OPERATIONAL_EVIDENCE value: $require_operational" >&2
    exit 2
    ;;
esac

scripts/verify-paper-environment.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m trading_ai.cli paper-auto-cycle \
  --lock-dir "$LOCK_DIR" \
  "${args[@]}"
