#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m unittest \
  tests.test_alpaca_paper_connection \
  tests.test_alpaca_paper_execution \
  tests.test_approved_data_evaluation \
  tests.test_data_catalog \
  tests.test_evaluation_registry \
  tests.test_futures_readiness_report \
  tests.test_futures_research_scaffold \
  tests.test_mlflow_registry_sync \
  tests.test_model_challenger_report \
  tests.test_model_review_cycle_report \
  tests.test_model_review_decision \
  tests.test_paper_audit \
  tests.test_paper_campaign_report \
  tests.test_paper_close_session \
  tests.test_paper_common \
  tests.test_paper_daily \
  tests.test_paper_day_close \
  tests.test_paper_evidence_index \
  tests.test_paper_execute_session \
  tests.test_paper_gate_scripts \
  tests.test_paper_monitor \
  tests.test_paper_observability \
  tests.test_paper_ops_check \
  tests.test_paper_ops_rehearsal \
  tests.test_paper_performance_report \
  tests.test_paper_preflight \
  tests.test_paper_session \
  tests.test_paper_statement_validate \
  tests.test_paper_weekly_summary \
  tests.test_prepare_paper_daily \
  -v
