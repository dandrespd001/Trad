#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ALPACA_MCP_ENV_FILE:-$ROOT_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi

: "${ALPACA_PAPER_API_KEY:?Set ALPACA_PAPER_API_KEY in $ENV_FILE}"
: "${ALPACA_PAPER_SECRET_KEY:?Set ALPACA_PAPER_SECRET_KEY in $ENV_FILE}"

export ALPACA_API_KEY="$ALPACA_PAPER_API_KEY"
export ALPACA_SECRET_KEY="$ALPACA_PAPER_SECRET_KEY"
export ALPACA_PAPER_TRADE=true
export ALPACA_TOOLSETS="${ALPACA_TOOLSETS:-account,assets,stock-data,crypto-data,options-data,corporate-actions,news,fixed-income-data,index-data}"

exec uvx alpaca-mcp-server --transport stdio "$@"
