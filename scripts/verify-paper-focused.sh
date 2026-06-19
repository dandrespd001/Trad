#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/lib/python-bin.sh
source "$ROOT/scripts/lib/python-bin.sh"
PYTHON_BIN="$(resolve_python_bin "$ROOT")"
export PYTHON_BIN

cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PYTHONPATH:-src}" "$PYTHON_BIN" -m unittest \
  tests.test_adaptive_training_cycle \
  tests.test_alpaca_paper_connection \
  tests.test_alpaca_paper_execution \
  tests.test_approved_data_evaluation \
  tests.test_data_catalog \
  tests.test_evaluation_registry \
  tests.test_futures_readiness_report \
  tests.test_futures_research_scaffold \
  tests.test_llm_paper_review \
  tests.test_llm_context_pack \
  tests.test_llm_signal_proposals \
  tests.test_market_data_providers \
  tests.test_mlflow_registry_sync \
  tests.test_model_challenger_report \
  tests.test_model_review_cycle_report \
  tests.test_model_review_decision \
  tests.test_paper_audit \
  tests.test_paper_adaptive_routing \
  tests.test_paper_auto_cycle \
  tests.test_paper_autopilot_plan \
  tests.test_paper_bot_cycle \
  tests.test_paper_campaign_report \
  tests.test_paper_challenger_shadow_plan \
  tests.test_paper_close_session \
  tests.test_paper_common \
  tests.test_paper_daily \
  tests.test_paper_day_close \
  tests.test_paper_evidence_index \
  tests.test_paper_execute_session \
  tests.test_paper_gate_scripts \
  tests.test_paper_monitor \
  tests.test_paper_observability \
  tests.test_paper_operator_status \
  tests.test_paper_ops_check \
  tests.test_paper_ops_rehearsal \
  tests.test_paper_performance_report \
  tests.test_paper_phase_review_report \
  tests.test_paper_preflight \
  tests.test_paper_review_decision \
  tests.test_paper_session \
  tests.test_paper_signal_arbitration \
  tests.test_paper_statement_validate \
  tests.test_paper_strategy_quality \
  tests.test_paper_weekly_summary \
  tests.test_prepare_paper_daily \
  -v
