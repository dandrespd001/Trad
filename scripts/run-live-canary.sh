#!/usr/bin/env bash
set -euo pipefail

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

PYTHONPATH="${PYTHONPATH:-src}" python3 -m trading_ai.cli live-canary \
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
