#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' \
  'alpaca-mcp-paper is disabled: it bypassed the single paper executor and exposed broker credentials.' \
  'Use the credential-free PaperExecutorBrokerClient; a future MCP adapter must be GET-only over that IPC boundary.' >&2
exit 64
