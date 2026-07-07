#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "missing required env: ${name}" >&2
    exit 2
  fi
}

require_env AS_OF_DATE
require_env SYMBOL
require_env REVIEWER
require_env REASON
require_env CONFIRM_LIVE_CANARY
require_env READINESS
require_env EXPECTED_READINESS_HASH
require_env BREAKER_STATE
require_env REHEARSAL_SUMMARY
require_env ROLLBACK_EVIDENCE

EXPECTED_CONFIRMATION="I confirm LIVE CANARY ${AS_OF_DATE} ${SYMBOL} USD 1 reviewer=${REVIEWER} reason=${REASON}"
if [[ "${CONFIRM_LIVE_CANARY}" != "${EXPECTED_CONFIRMATION}" ]]; then
  echo "confirmation mismatch" >&2
  echo "expected: ${EXPECTED_CONFIRMATION}" >&2
  exit 1
fi

ARGS=(
  live-canary
  --as-of-date "${AS_OF_DATE}" \
  --symbol "${SYMBOL}" \
  --notional-usd 1 \
  --readiness "${READINESS}" \
  --expected-readiness-hash "${EXPECTED_READINESS_HASH}" \
  --breaker-state "${BREAKER_STATE}" \
  --rehearsal-summary "${REHEARSAL_SUMMARY}" \
  --rollback-evidence "${ROLLBACK_EVIDENCE}" \
  --reviewer "${REVIEWER}" \
  --reason "${REASON}" \
  --confirmation "${CONFIRM_LIVE_CANARY}" \
  --output-dir "${OUTPUT_DIR:-reports/tmp/live_canary}" \
  --market-open-confirmed
)

if [[ "${ENABLE_REAL_SUBMIT:-}" == "YES_I_UNDERSTAND_LIVE_ORDER" ]]; then
  require_env RISK_LIVE
  require_env REFERENCE_PRICE
  require_env CONFIRM_LIVE_SUBMIT

  EXPECTED_REAL_CONFIRMATION="I confirm REAL LIVE SUBMIT ${AS_OF_DATE} ${SYMBOL} USD 1 readiness_hash=${EXPECTED_READINESS_HASH} reviewer=${REVIEWER} reason=${REASON}"
  if [[ "${CONFIRM_LIVE_SUBMIT}" != "${EXPECTED_REAL_CONFIRMATION}" ]]; then
    echo "real-submit confirmation mismatch" >&2
    echo "expected: ${EXPECTED_REAL_CONFIRMATION}" >&2
    exit 1
  fi

  ARGS+=(
    --enable-real-submit
    --risk-live "${RISK_LIVE}"
    --reference-price "${REFERENCE_PRICE}"
    --confirm-real-submit "${CONFIRM_LIVE_SUBMIT}"
    --universe "${UNIVERSE:-configs/universe.yml}"
  )
fi

if [[ -n "${AUTONOMY_STATE_DIR:-}" ]]; then
  ARGS+=(--autonomy-state-dir "${AUTONOMY_STATE_DIR}")
fi
if [[ -n "${AUTONOMY_MARKET:-}" ]]; then
  ARGS+=(--autonomy-market "${AUTONOMY_MARKET}")
fi
if [[ -n "${SIGNAL_PLAN:-}" ]]; then
  ARGS+=(--signal-plan "${SIGNAL_PLAN}")
fi
if [[ -n "${APPROVAL_REGISTRY_DIR:-}" ]]; then
  ARGS+=(--approval-registry-dir "${APPROVAL_REGISTRY_DIR}")
fi

cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m trading_ai.cli "${ARGS[@]}"
