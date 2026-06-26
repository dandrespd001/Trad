"""Command-line interface for the trading AI research MVP."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from trading_ai.backtest.engine import BacktestConfig, run_momentum_vol_target_backtest
from trading_ai.cli_paper import PaperCliHandlers, add_paper_subcommands
from trading_ai.config import ConfigError, load_risk_config, load_universe_config
from trading_ai.data.catalog import (
    ApprovedDataImportError,
    ApprovedDataValidationError,
    import_approved_data,
)
from trading_ai.data.freshness import evaluate_ohlcv_freshness
from trading_ai.data.io import ParquetDependencyError, read_records, write_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.data.market_data import ApprovedLocalMarketDataProvider, MarketDataRequest
from trading_ai.data.sample import generate_sample_ohlcv
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.evaluation.adaptive_training import AdaptiveTrainingOperationalError, run_adaptive_training_cycle
from trading_ai.evaluation.approved_data import ApprovedEvaluationOperationalError, evaluate_approved_data
from trading_ai.evaluation.model_challenger import ModelChallengerOperationalError, run_model_challenger_report
from trading_ai.evaluation.model_research import ModelResearchOperationalError, run_model_research_sweep
from trading_ai.evaluation.model_review_cycle import (
    ModelReviewCycleOperationalError,
    run_model_review_cycle_report,
)
from trading_ai.evaluation.model_review_decision import (
    DECISION_APPROVE,
    DECISION_DEFER,
    DECISION_REJECT,
    ModelReviewDecisionOperationalError,
    run_model_review_decision,
)
from trading_ai.evaluation.paper_daily_prepare import (
    PaperDailyPrepareOperationalError,
    prepare_paper_daily,
)
from trading_ai.evaluation.registry import EvaluationRegistryOperationalError, register_evaluation
from trading_ai.execution.alpaca_connection import build_alpaca_paper_client
from trading_ai.execution.alpaca_paper import (
    AlpacaPaperBroker,
    PaperOrder,
    PaperOrderResult,
    PaperOrderSnapshot,
    PaperPosition,
    PaperPreflightDecision,
    evaluate_paper_preflight,
)
from trading_ai.execution.futures_readiness import (
    DEFAULT_CONFIG as FUTURES_READINESS_DEFAULT_CONFIG,
)
from trading_ai.execution.futures_readiness import (
    DEFAULT_MARKDOWN_OUTPUT as FUTURES_READINESS_DEFAULT_MARKDOWN_OUTPUT,
)
from trading_ai.execution.futures_readiness import (
    DEFAULT_OUTPUT as FUTURES_READINESS_DEFAULT_OUTPUT,
)
from trading_ai.execution.futures_readiness import (
    FuturesReadinessOperationalError,
    run_futures_readiness_report,
)
from trading_ai.execution.futures_research import FuturesResearchOperationalError, run_futures_research_scaffold
from trading_ai.execution.live_canary import run_live_canary
from trading_ai.execution.live_execute_session import run_live_execute_session
from trading_ai.execution.live_readiness import run_live_readiness_report
from trading_ai.execution.live_rehearsal import run_live_rehearsal
from trading_ai.execution.llm_context_pack import LlmContextPackOperationalError, run_llm_context_pack
from trading_ai.execution.llm_paper_review import LlmPaperReviewOperationalError, run_llm_paper_review
from trading_ai.execution.llm_signal_proposals import (
    LLMSignalProposalsOperationalError,
    run_llm_signal_proposals,
)
from trading_ai.execution.paper_audit import evaluate_paper_audit, render_paper_audit_markdown
from trading_ai.execution.paper_auto_cycle import PaperAutoCycleOperationalError, run_paper_auto_cycle
from trading_ai.execution.paper_autopilot_plan import (
    PaperAutopilotPlanOperationalError,
    run_paper_autopilot_plan,
)
from trading_ai.execution.paper_bot_cycle import PaperBotCycleOperationalError, run_paper_bot_cycle
from trading_ai.execution.paper_campaign import (
    PaperCampaignOperationalError,
    build_paper_campaign_report,
    write_paper_campaign_report,
)
from trading_ai.execution.paper_challenger_shadow import (
    PaperChallengerShadowOperationalError,
    run_paper_challenger_shadow_plan,
)
from trading_ai.execution.paper_challenger_signals import (
    PaperChallengerSignalsOperationalError,
    run_paper_challenger_signals,
)
from trading_ai.execution.paper_close_session import PaperCloseOperationalError, run_paper_close_session
from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact
from trading_ai.execution.paper_daily import (
    DEFAULT_CONFIG_PATH as PAPER_DAILY_DEFAULT_CONFIG,
)
from trading_ai.execution.paper_daily import (
    PaperDailyOperationalError,
    load_paper_daily_config,
    run_paper_daily,
    run_paper_daily_from_readiness,
)
from trading_ai.execution.paper_day_close import PaperDayCloseOperationalError, run_paper_day_close
from trading_ai.execution.paper_evidence_index import (
    PaperEvidenceIndexOperationalError,
    run_paper_evidence_index,
)
from trading_ai.execution.paper_execute_session import PaperExecuteOperationalError, run_paper_execute_session
from trading_ai.execution.paper_graduation import load_optional_json_report
from trading_ai.execution.paper_model_alias import (
    run_paper_model_alias_decision,
)
from trading_ai.execution.paper_monitor import PaperMonitorOperationalError, run_paper_monitor
from trading_ai.execution.paper_observability import (
    append_paper_ledger_event,
    build_paper_observability_report,
    paper_closeout_ledger_event,
    paper_execution_ledger_event,
    paper_order_ledger_event,
    paper_session_ledger_event,
    write_paper_observability_report,
)
from trading_ai.execution.paper_operator_status import PaperOperatorStatusOperationalError, run_paper_operator_status
from trading_ai.execution.paper_ops_check import PaperOpsCheckOperationalError, run_paper_ops_check
from trading_ai.execution.paper_performance import PaperPerformanceOperationalError, run_paper_performance_report
from trading_ai.execution.paper_phase_review import PaperPhaseReviewOperationalError, run_paper_phase_review_report
from trading_ai.execution.paper_position_watch import (
    PaperPositionWatchOperationalError,
    run_paper_position_watch,
)
from trading_ai.execution.paper_rehearsal import PaperOpsRehearsalOperationalError, run_paper_ops_rehearsal
from trading_ai.execution.paper_review_decision import (
    PaperReviewDecisionOperationalError,
    run_paper_review_decision,
)
from trading_ai.execution.paper_safe_flatten import (
    PaperSafeFlattenOperationalError,
    run_paper_safe_flatten,
)
from trading_ai.execution.paper_session import run_offline_paper_session
from trading_ai.execution.paper_shadow_outcome import run_paper_shadow_outcome_report
from trading_ai.execution.paper_shadow_scorecard import run_paper_shadow_scorecard
from trading_ai.execution.paper_signal_arbitration import (
    PaperSignalArbitrationOperationalError,
    run_paper_signal_arbitration,
)
from trading_ai.execution.paper_statement import PaperStatementOperationalError, run_paper_statement_validate
from trading_ai.execution.paper_strategy_quality import PaperStrategyQualityOperationalError, run_paper_strategy_quality
from trading_ai.execution.paper_trial_day import run_paper_trial_day
from trading_ai.execution.paper_weekly_summary import PaperWeeklySummaryOperationalError, run_paper_weekly_summary
from trading_ai.features.engineering import build_features, default_model_feature_names
from trading_ai.llm.evals import run_guardrail_evals
from trading_ai.llm.factory import (
    run_llm_adaptive_review,
    run_llm_candidate_report,
    run_llm_eval_suite,
    run_llm_model_alias_decision,
    run_llm_role_registry,
    run_llm_supervise_labels,
    run_llm_training_dataset,
    run_llm_training_export,
)
from trading_ai.llm.local_registry import (
    DEFAULT_LOCAL_SMOKE_PROMPT,
    run_llm_local_adapter_report,
    run_llm_local_alias_decision,
    run_llm_local_cache_verify,
    run_llm_local_eval_suite,
    run_llm_local_sft,
    run_llm_local_smoke,
)
from trading_ai.models.baseline import (
    LogisticBaselineConfig,
    build_supervised_examples,
    evaluate_classifier,
    load_model,
    save_model,
    temporal_train_test_split,
    train_logistic_baseline,
    walk_forward_evaluate,
)
from trading_ai.models.promotion import PromotionPolicy, evaluate_promotion
from trading_ai.models.signals import ModelSignal, generate_model_signals, latest_valid_feature_rows
from trading_ai.monitoring.drift import evaluate_feature_drift, render_feature_drift_markdown
from trading_ai.reports.markdown import render_backtest_report


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-ai")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--config", default="configs/universe.yml")
    ingest.add_argument("--from", dest="start", required=True)
    ingest.add_argument("--to", dest="end", required=True)
    ingest.add_argument("--output", default="reports/tmp/ingest/latest.csv")
    ingest.add_argument("--source-csv")
    ingest.set_defaults(func=_ingest)

    import_approved = subparsers.add_parser("import-approved-data")
    import_approved.add_argument("--source", required=True)
    import_approved.add_argument("--dataset-id", required=True)
    import_approved.add_argument("--frequency", required=True, choices=("1d", "1h"))
    import_approved.add_argument("--config", default="configs/universe.yml")
    import_approved.add_argument("--provider", required=True)
    import_approved.add_argument("--license-note", required=True)
    import_approved.add_argument("--output-dir", default="data/raw/approved")
    import_approved.add_argument("--as-of-date", required=True)
    import_approved.set_defaults(func=_import_approved_data)

    evaluate_approved = subparsers.add_parser("evaluate-approved-data")
    evaluate_approved.add_argument("--approved-dir", required=True)
    evaluate_approved.add_argument("--config", default="configs/universe.yml")
    evaluate_approved.add_argument("--risk", default="configs/risk.yml")
    evaluate_approved.add_argument("--output-dir", default="reports/tmp/approved_eval")
    evaluate_approved.add_argument("--as-of-date", required=True)
    evaluate_approved.add_argument("--periods-per-year", default="auto")
    evaluate_approved.add_argument("--min-accuracy-lift", type=float, default=0.02)
    evaluate_approved.add_argument("--min-test-samples", type=int, default=30)
    evaluate_approved.add_argument("--candidate-spec")
    evaluate_approved.set_defaults(func=_evaluate_approved_data)

    model_research_sweep = subparsers.add_parser("model-research-sweep")
    model_research_sweep.add_argument("--approved-dir", required=True)
    model_research_sweep.add_argument("--from", dest="start", required=True)
    model_research_sweep.add_argument("--to", dest="end", required=True)
    model_research_sweep.add_argument("--as-of-date", required=True)
    model_research_sweep.add_argument("--config", default="configs/universe.yml")
    model_research_sweep.add_argument("--risk", default="configs/risk.yml")
    model_research_sweep.add_argument("--output-dir", default="reports/tmp/model_research")
    model_research_sweep.add_argument("--min-accuracy-lift", type=float, default=0.02)
    model_research_sweep.add_argument("--min-test-samples", type=int, default=30)
    model_research_sweep.set_defaults(func=_model_research_sweep)

    register_evaluation_parser = subparsers.add_parser("register-evaluation")
    register_evaluation_parser.add_argument("--evaluation-dir", required=True)
    register_evaluation_parser.add_argument("--registry-dir", default="reports/registry")
    register_evaluation_parser.set_defaults(func=_register_evaluation)

    sync_registry_mlflow = subparsers.add_parser("sync-registry-mlflow")
    sync_registry_mlflow.add_argument("--registry-dir", default="reports/registry")
    sync_registry_mlflow.add_argument("--tracking-uri", default="reports/mlruns")
    sync_registry_mlflow.add_argument("--experiment-name", default="approved-data-evaluations")
    sync_registry_mlflow.add_argument("--run-id")
    sync_registry_mlflow.set_defaults(func=_sync_registry_mlflow)

    register_registry_mlflow_model = subparsers.add_parser("register-registry-mlflow-model")
    register_registry_mlflow_model.add_argument("--run-id", required=True)
    register_registry_mlflow_model.add_argument("--registry-dir", default="reports/registry")
    register_registry_mlflow_model.add_argument("--tracking-uri", default="reports/mlruns")
    register_registry_mlflow_model.add_argument("--experiment-name", default="approved-data-evaluations")
    register_registry_mlflow_model.add_argument(
        "--registered-model-name",
        default="approved-data-logistic-baseline",
    )
    register_registry_mlflow_model.add_argument("--alias", default="paper-candidate")
    register_registry_mlflow_model.set_defaults(func=_register_registry_mlflow_model)

    review_mlflow_paper_candidate = subparsers.add_parser("review-mlflow-paper-candidate")
    review_mlflow_paper_candidate.add_argument("--registry-dir", default="reports/registry")
    review_mlflow_paper_candidate.add_argument("--tracking-uri", default="reports/mlruns")
    review_mlflow_paper_candidate.add_argument(
        "--registered-model-name",
        default="approved-data-logistic-baseline",
    )
    review_mlflow_paper_candidate.add_argument("--alias", default="paper-candidate")
    review_mlflow_paper_candidate.add_argument("--features", default="data/processed/features.csv")
    review_mlflow_paper_candidate.add_argument("--config", default="configs/universe.yml")
    review_mlflow_paper_candidate.add_argument(
        "--output",
        default="reports/tmp/mlflow_paper_candidate_review/latest.json",
    )
    review_mlflow_paper_candidate.add_argument(
        "--markdown-output",
        default="reports/tmp/mlflow_paper_candidate_review/latest.md",
    )
    review_mlflow_paper_candidate.set_defaults(func=_review_mlflow_paper_candidate)

    model_challenger = subparsers.add_parser("model-challenger-report")
    model_challenger.add_argument("--evaluation-dir", required=True)
    model_challenger.add_argument("--paper-performance")
    model_challenger.add_argument("--mlflow-review")
    model_challenger.add_argument("--phase-review")
    model_challenger.add_argument("--training-cycle")
    model_challenger.add_argument("--output-dir", default="reports/tmp/model_challenger")
    model_challenger.set_defaults(func=_model_challenger_report)

    adaptive_training = subparsers.add_parser("adaptive-training-cycle")
    adaptive_training.add_argument("--as-of-date", required=True)
    adaptive_training.add_argument("--approved-dir", required=True)
    adaptive_training.add_argument("--phase-review", required=True)
    adaptive_training.add_argument("--paper-performance", required=True)
    adaptive_training.add_argument("--registry-dir", required=True)
    adaptive_training.add_argument("--cadence", default="weekly", choices=("weekly", "daily", "manual"))
    adaptive_training.add_argument("--force", action="store_true")
    adaptive_training.add_argument("--output-dir", default="reports/tmp/adaptive_training")
    adaptive_training.set_defaults(func=_adaptive_training_cycle)

    model_review_decision = subparsers.add_parser("model-review-decision")
    model_review_decision.add_argument("--challenger-report", required=True)
    model_review_decision.add_argument(
        "--decision",
        required=True,
        choices=(DECISION_APPROVE, DECISION_REJECT, DECISION_DEFER),
    )
    model_review_decision.add_argument("--reviewer", required=True)
    model_review_decision.add_argument("--reason", required=True)
    model_review_decision.add_argument("--output-dir", default="reports/tmp/model_challenger_decisions")
    model_review_decision.set_defaults(func=_model_review_decision)

    model_review_cycle = subparsers.add_parser("model-review-cycle-report")
    model_review_cycle.add_argument("--challenger-report", required=True)
    model_review_cycle.add_argument("--review-decision", required=True)
    model_review_cycle.add_argument("--output-dir", default="reports/tmp/model_challenger_cycles")
    model_review_cycle.set_defaults(func=_model_review_cycle_report)

    refresh = subparsers.add_parser("refresh-data")
    refresh.add_argument("--source-csv", "--source", dest="source_csv", required=True)
    refresh.add_argument("--from", dest="start", required=True)
    refresh.add_argument("--to", dest="end", required=True)
    refresh.add_argument("--config", default="configs/universe.yml")
    refresh.add_argument("--signal-model", default="models/latest_model.json")
    refresh.add_argument("--output-dir", default="reports/tmp/fresh_data")
    refresh.add_argument("--max-age-days", type=int, default=5)
    refresh.add_argument("--as-of-date")
    refresh.set_defaults(func=_refresh_data)

    validate = subparsers.add_parser("validate-data")
    validate.add_argument("--dataset", required=True)
    validate.set_defaults(func=_validate_data)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--dataset", required=True)
    manifest.add_argument("--output", required=True)
    manifest.set_defaults(func=_manifest)

    features = subparsers.add_parser("build-features")
    features.add_argument("--dataset", required=True)
    features.add_argument("--output", default="reports/tmp/build_features/latest.csv")
    features.set_defaults(func=_build_features)

    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("--strategy", default="momentum-vol-target")
    backtest.add_argument("--config", default="configs/risk.yml")
    backtest.add_argument("--dataset", default="data/raw/etfs.csv")
    backtest.add_argument("--output", default="reports/tmp/backtest/latest.json")
    backtest.add_argument("--report-output", default="reports/tmp/backtest/latest.md")
    backtest.set_defaults(func=_backtest)

    train = subparsers.add_parser("train")
    train.add_argument("--model", required=True)
    train.add_argument("--config", default="configs/model.yml")
    train.add_argument("--dataset", default="data/processed/features.csv")
    train.add_argument("--output", default="reports/tmp/train/latest_model.json")
    train.add_argument("--run-output", default="reports/tmp/train/latest_run.json")
    train.set_defaults(func=_train)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--run-id", required=True)
    evaluate.add_argument("--output", default="reports/tmp/evaluate/latest.json")
    evaluate.set_defaults(func=_evaluate)

    promote = subparsers.add_parser("promote")
    promote.add_argument("--run-id", required=True)
    promote.add_argument("--baseline", required=True)
    promote.add_argument("--output", default="reports/tmp/promote/latest.json")
    promote.add_argument("--min-accuracy-lift", type=float, default=0.02)
    promote.add_argument("--min-test-samples", type=int, default=30)
    promote.set_defaults(func=_promote)

    llm_eval = subparsers.add_parser("llm-eval")
    llm_eval.add_argument("--output", default="reports/tmp/llm_eval/latest.json")
    llm_eval.set_defaults(func=_llm_eval)

    llm_roles = subparsers.add_parser("llm-role-registry")
    llm_roles.add_argument("--output-dir", default="reports/tmp/llm_roles")
    llm_roles.set_defaults(func=_llm_role_registry)

    llm_dataset = subparsers.add_parser("llm-training-dataset")
    llm_dataset.add_argument("--role", required=True)
    llm_dataset.add_argument("--as-of-date", required=True)
    llm_dataset.add_argument("--source-root", required=True)
    llm_dataset.add_argument("--output-dir", default="reports/tmp/llm_training")
    llm_dataset.set_defaults(func=_llm_training_dataset)

    llm_supervise = subparsers.add_parser("llm-supervise-labels")
    llm_supervise.add_argument("--role", required=True)
    llm_supervise.add_argument("--dataset", required=True)
    llm_supervise.add_argument("--frontier-model", required=True)
    llm_supervise.add_argument("--output-dir", default="reports/tmp/llm_supervision")
    llm_supervise.add_argument("--use-openai", action="store_true")
    llm_supervise.add_argument("--confirm-llm-supervision", action="store_true")
    llm_supervise.set_defaults(func=_llm_supervise_labels)

    llm_eval_suite = subparsers.add_parser("llm-eval-suite")
    llm_eval_suite.add_argument("--role", required=True)
    llm_eval_suite.add_argument("--candidate", required=True)
    llm_eval_suite.add_argument("--holdout", required=True)
    llm_eval_suite.add_argument("--output-dir", default="reports/tmp/llm_evals")
    llm_eval_suite.set_defaults(func=_llm_eval_suite)

    llm_candidate = subparsers.add_parser("llm-candidate-report")
    llm_candidate.add_argument("--role", required=True)
    llm_candidate.add_argument("--baseline-eval", required=True)
    llm_candidate.add_argument("--candidate-eval", required=True)
    llm_candidate.add_argument("--output-dir", default="reports/tmp/llm_candidates")
    llm_candidate.set_defaults(func=_llm_candidate_report)

    llm_export = subparsers.add_parser("llm-training-export")
    llm_export.add_argument("--role", required=True)
    llm_export.add_argument("--supervised-dataset", required=True)
    llm_export.add_argument(
        "--format",
        dest="output_format",
        default="trl-jsonl",
        choices=("trl-jsonl", "openai-jsonl"),
    )
    llm_export.add_argument("--output-dir", default="reports/tmp/llm_training_export")
    llm_export.set_defaults(func=_llm_training_export)

    llm_local_cache = subparsers.add_parser("llm-local-cache-verify")
    llm_local_cache.add_argument("--model-id", required=True)
    llm_local_cache.add_argument("--registry", default="configs/llm_local_models.json")
    llm_local_cache.add_argument("--cache-root", default="models/local/weights")
    llm_local_cache.add_argument("--output", default="reports/tmp/llm_local/cache_verify.json")
    llm_local_cache.set_defaults(func=_llm_local_cache_verify)

    llm_local_smoke = subparsers.add_parser("llm-local-smoke")
    llm_local_smoke.add_argument("--model-id", required=True)
    llm_local_smoke.add_argument("--registry", default="configs/llm_local_models.json")
    llm_local_smoke.add_argument("--cache-root", default="models/local/weights")
    llm_local_smoke.add_argument("--schema-name", default="PaperOpsReview")
    llm_local_smoke.add_argument("--prompt", default=DEFAULT_LOCAL_SMOKE_PROMPT)
    llm_local_smoke.add_argument("--max-new-tokens", type=int, default=256)
    llm_local_smoke.add_argument("--fixture-response", help=argparse.SUPPRESS)
    llm_local_smoke.add_argument("--adapter-manifest")
    llm_local_smoke.add_argument("--output", default="reports/tmp/llm_local/smoke.json")
    llm_local_smoke.set_defaults(func=_llm_local_smoke)

    llm_local_sft = subparsers.add_parser("llm-local-sft")
    llm_local_sft.add_argument("--role", required=True)
    llm_local_sft.add_argument("--base-model-id", required=True)
    llm_local_sft.add_argument("--training-jsonl", required=True)
    llm_local_sft.add_argument("--adapter-dir", required=True)
    llm_local_sft.add_argument("--registry", default="configs/llm_local_models.json")
    llm_local_sft.add_argument("--cache-root", default="models/local/weights")
    llm_local_sft.add_argument("--metrics-json")
    llm_local_sft.add_argument("--register-existing-adapter", action="store_true")
    llm_local_sft.add_argument("--epochs", type=float, default=1.0)
    llm_local_sft.add_argument("--learning-rate", type=float, default=2e-4)
    llm_local_sft.add_argument("--batch-size", type=int, default=1)
    llm_local_sft.add_argument("--gradient-accumulation-steps", type=int, default=1)
    llm_local_sft.add_argument("--max-steps", type=int, default=-1)
    llm_local_sft.add_argument("--lora-rank", type=int, default=8)
    llm_local_sft.add_argument("--lora-alpha", type=int, default=16)
    llm_local_sft.add_argument("--lora-dropout", type=float, default=0.05)
    llm_local_sft.add_argument("--dtype", default="auto")
    llm_local_sft.add_argument("--device", default="auto")
    llm_local_sft.add_argument("--output", default="reports/tmp/llm_local_sft/manifest.json")
    llm_local_sft.set_defaults(func=_llm_local_sft)

    llm_local_eval = subparsers.add_parser("llm-local-eval-suite")
    llm_local_eval.add_argument("--role", required=True)
    llm_local_eval.add_argument("--candidate", required=True)
    llm_local_eval.add_argument("--holdout", required=True)
    llm_local_eval.add_argument("--base-model-id", required=True)
    llm_local_eval.add_argument("--adapter-manifest", required=True)
    llm_local_eval.add_argument("--output-dir", default="reports/tmp/llm_local_eval_suite")
    llm_local_eval.set_defaults(func=_llm_local_eval_suite)

    llm_local_adapter = subparsers.add_parser("llm-local-adapter-report")
    llm_local_adapter.add_argument("--role", required=True)
    llm_local_adapter.add_argument("--sft-manifest", required=True)
    llm_local_adapter.add_argument("--eval-report", required=True)
    llm_local_adapter.add_argument("--smoke-report")
    llm_local_adapter.add_argument("--output-dir", default="reports/tmp/llm_local_adapters")
    llm_local_adapter.set_defaults(func=_llm_local_adapter_report)

    llm_local_alias = subparsers.add_parser("llm-local-alias-decision")
    llm_local_alias.add_argument("--role", required=True)
    llm_local_alias.add_argument("--adapter-report", required=True)
    llm_local_alias.add_argument("--reviewer", required=True)
    llm_local_alias.add_argument("--reason", required=True)
    llm_local_alias.add_argument("--decision", required=True, choices=("APPROVE", "REJECT", "DEFER"))
    llm_local_alias.add_argument("--ttl-days", type=int, default=30)
    llm_local_alias.add_argument("--output-dir", default="reports/tmp/llm_local_alias")
    llm_local_alias.set_defaults(func=_llm_local_alias_decision)

    llm_alias = subparsers.add_parser("llm-model-alias-decision")
    llm_alias.add_argument("--role", required=True)
    llm_alias.add_argument("--candidate-report", required=True)
    llm_alias.add_argument("--reviewer", required=True)
    llm_alias.add_argument("--reason", required=True)
    llm_alias.add_argument("--decision", required=True, choices=("APPROVE", "REJECT", "DEFER"))
    llm_alias.add_argument("--ttl-days", type=int, default=30)
    llm_alias.add_argument("--output-dir", default="reports/tmp/llm_model_alias")
    llm_alias.set_defaults(func=_llm_model_alias_decision)

    llm_adaptive = subparsers.add_parser("llm-adaptive-review")
    llm_adaptive.add_argument("--role", required=True)
    llm_adaptive.add_argument("--feedback-ledger", required=True)
    llm_adaptive.add_argument("--eval-report", required=True)
    llm_adaptive.add_argument("--output-dir", default="reports/tmp/llm_adaptive_review")
    llm_adaptive.add_argument("--min-corrections-for-supervision", type=int, default=3)
    llm_adaptive.set_defaults(func=_llm_adaptive_review)

    report = subparsers.add_parser("report")
    report.add_argument("--run-id", default="reports/tmp/backtest/latest.json")
    report.add_argument("--output", default="reports/tmp/report/latest.md")
    report.set_defaults(func=_report)

    drift_report = subparsers.add_parser("drift-report")
    drift_report.add_argument("--reference-features", required=True)
    drift_report.add_argument("--current-features", required=True)
    drift_report.add_argument("--feature-names")
    drift_report.add_argument("--output", default="reports/tmp/monitoring/latest_drift.json")
    drift_report.add_argument("--markdown-output", default="reports/tmp/monitoring/latest_drift.md")
    drift_report.add_argument("--mean-z-threshold", type=float, default=2.0)
    drift_report.add_argument("--missing-delta-threshold", type=float, default=0.10)
    drift_report.add_argument("--std-ratio-threshold", type=float, default=2.0)
    drift_report.add_argument("--min-samples", type=int, default=20)
    drift_report.set_defaults(func=_drift_report)

    live_readiness = subparsers.add_parser("live-readiness-report")
    live_readiness.add_argument("--as-of-date", required=True)
    live_readiness.add_argument("--phase-review", required=True)
    live_readiness.add_argument("--campaign-report", required=True)
    live_readiness.add_argument("--performance-report", required=True)
    live_readiness.add_argument("--permissions", required=True)
    live_readiness.add_argument("--reviewer", required=True)
    live_readiness.add_argument("--reason", required=True)
    live_readiness.add_argument("--output-dir", default="reports/tmp/live_readiness")
    live_readiness.set_defaults(func=_live_readiness_report)

    live_execute = subparsers.add_parser("live-execute-session")
    live_execute.add_argument("--as-of-date", required=True)
    live_execute.add_argument("--readiness", required=True)
    live_execute.add_argument("--risk", required=True)
    live_execute.add_argument("--expected-readiness-hash")
    live_execute.add_argument("--reviewer", required=True)
    live_execute.add_argument("--reason", required=True)
    live_execute.add_argument("--output-dir", default="reports/tmp/live_execute_session")
    live_execute.set_defaults(func=_live_execute_session, dry_run=True)

    live_canary = subparsers.add_parser("live-canary")
    live_canary.add_argument("--as-of-date", required=True)
    live_canary.add_argument("--symbol", required=True)
    live_canary.add_argument("--notional-usd", type=float, required=True)
    live_canary.add_argument("--readiness", required=True)
    live_canary.add_argument("--expected-readiness-hash", required=True)
    live_canary.add_argument("--breaker-state", required=True)
    live_canary.add_argument("--rehearsal-summary", required=True)
    live_canary.add_argument("--rollback-evidence", required=True)
    live_canary.add_argument("--reviewer", required=True)
    live_canary.add_argument("--reason", required=True)
    live_canary.add_argument("--confirmation", required=True)
    live_canary.add_argument("--output-dir", default="reports/tmp/live_canary")
    live_canary.add_argument("--market-open-confirmed", action="store_true")
    live_canary.add_argument("--enable-real-submit", action="store_true")
    live_canary.set_defaults(func=_live_canary)

    live_rehearsal = subparsers.add_parser("live-rehearsal")
    live_rehearsal.add_argument("--fixtures", required=True)
    live_rehearsal.add_argument("--output", required=True)
    live_rehearsal.set_defaults(func=_live_rehearsal)

    add_paper_subcommands(
        subparsers,
        handlers=PaperCliHandlers(
            paper=_paper,
            paper_audit=_paper_audit,
            paper_session=_paper_session,
            paper_execute_session=_paper_execute_session,
            paper_position_watch=_paper_position_watch,
            paper_safe_flatten=_paper_safe_flatten,
            paper_close_session=_paper_close_session,
            paper_observability=_paper_observability,
            paper_monitor=_paper_monitor,
            paper_campaign_report=_paper_campaign_report,
            paper_day_close=_paper_day_close,
            paper_performance_report=_paper_performance_report,
            paper_statement_validate=_paper_statement_validate,
            paper_weekly_summary=_paper_weekly_summary,
            paper_operator_status=_paper_operator_status,
            paper_strategy_quality=_paper_strategy_quality,
            paper_phase_review_report=_paper_phase_review_report,
            paper_trial_day=_paper_trial_day,
            paper_ops_check=_paper_ops_check,
            paper_ops_rehearsal=_paper_ops_rehearsal,
            paper_evidence_index=_paper_evidence_index,
            paper_daily=_paper_daily,
            paper_daily_from_readiness=_paper_daily_from_readiness,
            prepare_paper_daily=_prepare_paper_daily,
            llm_paper_review=_llm_paper_review,
            llm_signal_proposals=_llm_signal_proposals,
            paper_signal_arbitration=_paper_signal_arbitration,
            paper_challenger_shadow_plan=_paper_challenger_shadow_plan,
            paper_challenger_signals=_paper_challenger_signals,
            paper_shadow_outcome_report=_paper_shadow_outcome_report,
            paper_shadow_scorecard=_paper_shadow_scorecard,
            paper_model_alias_decision=_paper_model_alias_decision,
            paper_autopilot_plan=_paper_autopilot_plan,
            paper_review_decision=_paper_review_decision,
            paper_bot_cycle=_paper_bot_cycle,
            paper_auto_cycle=_paper_auto_cycle,
            llm_context_pack=_llm_context_pack,
        ),
        paper_daily_default_config=PAPER_DAILY_DEFAULT_CONFIG,
    )

    futures_readiness = subparsers.add_parser("futures-readiness-report")
    futures_readiness.add_argument("--config", default=FUTURES_READINESS_DEFAULT_CONFIG)
    futures_readiness.add_argument("--output", default=FUTURES_READINESS_DEFAULT_OUTPUT)
    futures_readiness.add_argument("--markdown-output", default=FUTURES_READINESS_DEFAULT_MARKDOWN_OUTPUT)
    futures_readiness.set_defaults(func=_futures_readiness_report)

    futures_research = subparsers.add_parser("futures-research-scaffold")
    futures_research.add_argument("--config", default=FUTURES_READINESS_DEFAULT_CONFIG)
    futures_research.add_argument("--output-dir", default="reports/tmp/futures_research")
    futures_research.add_argument("--as-of-date", required=True)
    futures_research.set_defaults(func=_futures_research_scaffold)
    return parser


def _ingest(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if args.source_csv:
        records = read_records(args.source_csv)
    else:
        universe = load_universe_config(args.config)
        records = generate_sample_ohlcv(symbols=universe.symbols, start=args.start, end=args.end)
    write_records(records, output)
    print(f"wrote {len(records)} rows to {output}")
    return 0


def _import_approved_data(args: argparse.Namespace) -> int:
    try:
        result = import_approved_data(
            source=args.source,
            dataset_id=args.dataset_id,
            frequency=args.frequency,
            config=args.config,
            provider=args.provider,
            license_note=args.license_note,
            output_dir=args.output_dir,
            as_of_date=args.as_of_date,
        )
    except ApprovedDataValidationError as exc:
        for error in exc.errors:
            print(error, file=sys.stderr)
        return 1
    except (ApprovedDataImportError, ParquetDependencyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote approved dataset to {result.dataset_path}")
    print(f"wrote approved manifest to {result.manifest_path}")
    print(f"wrote catalog entry to {result.catalog_entry_path}")
    return 0


def _evaluate_approved_data(args: argparse.Namespace) -> int:
    try:
        result = evaluate_approved_data(
            approved_dir=args.approved_dir,
            config=args.config,
            risk=args.risk,
            output_dir=args.output_dir,
            as_of_date=args.as_of_date,
            periods_per_year=args.periods_per_year,
            min_accuracy_lift=args.min_accuracy_lift,
            min_test_samples=args.min_test_samples,
            candidate_spec=args.candidate_spec,
        )
    except (ApprovedEvaluationOperationalError, ParquetDependencyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote approved evaluation to {result.output_dir}")
    print(f"wrote evaluation summary to {result.summary_path}")
    if result.exit_code != 0:
        print(f"evaluate-approved-data {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _model_research_sweep(args: argparse.Namespace) -> int:
    try:
        result = run_model_research_sweep(
            approved_dir=args.approved_dir,
            start=args.start,
            end=args.end,
            as_of_date=args.as_of_date,
            config=args.config,
            risk=args.risk,
            output_dir=args.output_dir,
            min_accuracy_lift=args.min_accuracy_lift,
            min_test_samples=args.min_test_samples,
        )
    except (ConfigError, ModelResearchOperationalError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote model research sweep report to {result.report_path}")
    print(f"wrote model research sweep markdown to {result.markdown_path}")
    print(f"wrote model research candidate specs to {result.candidate_specs_path}")
    if result.best_candidate_spec_path is not None:
        print(f"wrote best candidate spec to {result.best_candidate_spec_path}")
    if result.deployment_model_path is not None:
        print(f"wrote deployment model to {result.deployment_model_path}")
    if result.exit_code != 0:
        print(f"model-research-sweep {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _register_evaluation(args: argparse.Namespace) -> int:
    try:
        result = register_evaluation(
            evaluation_dir=args.evaluation_dir,
            registry_dir=args.registry_dir,
        )
    except EvaluationRegistryOperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"registered evaluation run {result.run_id}")
    print(f"wrote registry run to {result.run_path}")
    print(f"wrote registry index to {result.index_path}")
    return 0


def _sync_registry_mlflow(args: argparse.Namespace) -> int:
    from trading_ai.evaluation.mlflow_adapter import (
        MlflowRegistrySyncOperationalError,
        sync_registry_to_mlflow,
    )

    try:
        result = sync_registry_to_mlflow(
            registry_dir=args.registry_dir,
            tracking_uri=args.tracking_uri,
            experiment_name=args.experiment_name,
            run_id=args.run_id,
        )
    except MlflowRegistrySyncOperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        "synced registry to MLflow: "
        f"read={result.runs_read} created={result.created} "
        f"updated={result.updated} skipped={result.skipped}"
    )
    print(f"tracking URI: {result.tracking_uri}")
    print(f"experiment: {result.experiment_name}")
    return 0


def _register_registry_mlflow_model(args: argparse.Namespace) -> int:
    from trading_ai.evaluation.mlflow_model_registry import (
        MlflowModelRegistryOperationalError,
        register_registry_mlflow_model,
    )

    try:
        result = register_registry_mlflow_model(
            run_id=args.run_id,
            registry_dir=args.registry_dir,
            tracking_uri=args.tracking_uri,
            experiment_name=args.experiment_name,
            registered_model_name=args.registered_model_name,
            alias=args.alias,
        )
    except MlflowModelRegistryOperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    status = "created" if result.created else "reused"
    print(
        "registered registry MLflow model: "
        f"run={result.registry_run_id} model={result.registered_model_name} "
        f"version={result.model_version} alias={result.alias} {status}"
    )
    print(f"tracking URI: {result.tracking_uri}")
    print(f"experiment: {result.experiment_name}")
    return 0


def _review_mlflow_paper_candidate(args: argparse.Namespace) -> int:
    from trading_ai.evaluation.mlflow_paper_candidate_review import (
        MlflowPaperCandidateOperationalError,
        MlflowPaperCandidateValidationError,
        review_mlflow_paper_candidate,
    )

    try:
        result = review_mlflow_paper_candidate(
            registry_dir=args.registry_dir,
            tracking_uri=args.tracking_uri,
            registered_model_name=args.registered_model_name,
            alias=args.alias,
            features=args.features,
            config=args.config,
            output=args.output,
            markdown_output=args.markdown_output,
        )
    except MlflowPaperCandidateValidationError as exc:
        failures = exc.result.report.get("failures")
        if isinstance(failures, list):
            for failure in failures:
                print(str(failure), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        print(f"wrote MLflow paper candidate review to {exc.result.output_path}")
        print(f"wrote MLflow paper candidate review markdown to {exc.result.markdown_path}")
        return 1
    except MlflowPaperCandidateOperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"MLflow paper candidate review {str(result.report.get('status')).lower()}")
    print(f"wrote MLflow paper candidate review to {result.output_path}")
    print(f"wrote MLflow paper candidate review markdown to {result.markdown_path}")
    return 0


def _model_challenger_report(args: argparse.Namespace) -> int:
    try:
        result = run_model_challenger_report(
            evaluation_dir=args.evaluation_dir,
            paper_performance=args.paper_performance,
            mlflow_review=args.mlflow_review,
            phase_review=args.phase_review,
            training_cycle=args.training_cycle,
            output_dir=args.output_dir,
        )
    except (ModelChallengerOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote model challenger report to {result.output_path}")
    print(f"wrote model challenger markdown to {result.markdown_path}")
    if result.status in {"REJECTED", "BLOCKED", "ERROR"}:
        print(f"model challenger {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _adaptive_training_cycle(args: argparse.Namespace) -> int:
    try:
        result = run_adaptive_training_cycle(
            as_of_date=args.as_of_date,
            approved_dir=args.approved_dir,
            phase_review=args.phase_review,
            paper_performance=args.paper_performance,
            registry_dir=args.registry_dir,
            cadence=args.cadence,
            force=args.force,
            output_dir=args.output_dir,
        )
    except (AdaptiveTrainingOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote adaptive training cycle to {result.output_path}")
    print(f"wrote adaptive training markdown to {result.markdown_path}")
    print(f"appended adaptive training ledger to {result.ledger_path}")
    if result.training_state == "BLOCKED":
        print("adaptive training cycle blocked", file=sys.stderr)
    return result.exit_code


def _model_review_decision(args: argparse.Namespace) -> int:
    try:
        result = run_model_review_decision(
            challenger_report=args.challenger_report,
            decision=args.decision,
            reviewer=args.reviewer,
            reason=args.reason,
            output_dir=args.output_dir,
        )
    except (ModelReviewDecisionOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote model review decision to {result.output_path}")
    print(f"wrote model review decision markdown to {result.markdown_path}")
    if result.status == "ERROR":
        print("model review decision error", file=sys.stderr)
    return result.exit_code


def _model_review_cycle_report(args: argparse.Namespace) -> int:
    try:
        result = run_model_review_cycle_report(
            challenger_report=args.challenger_report,
            review_decision=args.review_decision,
            output_dir=args.output_dir,
        )
    except (ModelReviewCycleOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote model review cycle report to {result.output_path}")
    print(f"wrote model review cycle markdown to {result.markdown_path}")
    if result.status == "ERROR":
        print("model review cycle error", file=sys.stderr)
    return result.exit_code


def _refresh_data(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    as_of_date = _parse_cli_date(args.as_of_date) if args.as_of_date else date.today()
    universe = load_universe_config(args.config)
    raw_path = output_dir / "raw.csv"
    features_path = output_dir / "features.csv"
    raw_manifest_path = output_dir / "raw_manifest.json"
    features_manifest_path = output_dir / "features_manifest.json"
    freshness_path = output_dir / "freshness.json"
    request_payload = {
        "symbols": list(universe.symbols),
        "start": args.start,
        "end": args.end,
    }

    provider = ApprovedLocalMarketDataProvider(args.source_csv)
    try:
        raw_records = provider.load(MarketDataRequest(symbols=universe.symbols, start=args.start, end=args.end))
    except ValueError as exc:
        raw_manifest = _refresh_manifest(
            [],
            source=str(args.source_csv),
            dataset_path=raw_path,
            request=request_payload,
        )
        raw_manifest_path.write_text(json.dumps(raw_manifest, indent=2, sort_keys=True), encoding="utf-8")
        freshness_payload = evaluate_ohlcv_freshness(
            [],
            expected_symbols=universe.symbols,
            as_of_date=as_of_date,
            max_age_days=args.max_age_days,
        ).to_dict()
        freshness_payload["allowed"] = False
        freshness_payload["validation"] = {
            "valid": False,
            "errors": [str(exc)],
        }
        _write_refresh_freshness(
            freshness_payload,
            freshness_path=freshness_path,
            model_path=args.signal_model,
            feature_names=(),
            raw_path=raw_path,
            features_path=features_path,
        )
        print(str(exc), file=sys.stderr)
        return 1
    raw_manifest = _refresh_manifest(
        raw_records,
        source=str(args.source_csv),
        dataset_path=raw_path,
        request=request_payload,
    )
    raw_manifest_path.write_text(json.dumps(raw_manifest, indent=2, sort_keys=True), encoding="utf-8")

    if not raw_records:
        freshness_result = evaluate_ohlcv_freshness(
            [],
            expected_symbols=universe.symbols,
            as_of_date=as_of_date,
            max_age_days=args.max_age_days,
        )
        _write_refresh_freshness(
            freshness_result.to_dict(),
            freshness_path=freshness_path,
            model_path=args.signal_model,
            feature_names=(),
            raw_path=raw_path,
            features_path=features_path,
        )
        print("refresh-data blocked: empty_dataset", file=sys.stderr)
        return 1

    write_records(raw_records, raw_path)
    validation = validate_ohlcv_records(raw_records)
    if not validation.valid:
        freshness_payload = evaluate_ohlcv_freshness(
            raw_records,
            expected_symbols=universe.symbols,
            as_of_date=as_of_date,
            max_age_days=args.max_age_days,
        ).to_dict()
        freshness_payload["allowed"] = False
        freshness_payload["validation"] = {
            "valid": False,
            "errors": list(validation.errors),
        }
        _write_refresh_freshness(
            freshness_payload,
            freshness_path=freshness_path,
            model_path=args.signal_model,
            feature_names=(),
            raw_path=raw_path,
            features_path=features_path,
        )
        for error in validation.errors:
            print(error, file=sys.stderr)
        return 1

    feature_records = build_features(raw_records)
    write_records(feature_records, features_path)
    features_manifest = _refresh_manifest(
        feature_records,
        source=str(raw_path),
        dataset_path=features_path,
        request=request_payload,
    )
    features_manifest_path.write_text(json.dumps(features_manifest, indent=2, sort_keys=True), encoding="utf-8")

    model = load_model(args.signal_model)
    latest_rows = latest_valid_feature_rows(
        feature_records,
        feature_names=model.feature_names,
        allowlist=universe.symbols,
    )
    freshness_result = evaluate_ohlcv_freshness(
        latest_rows.values(),
        expected_symbols=universe.symbols,
        as_of_date=as_of_date,
        max_age_days=args.max_age_days,
    )
    _write_refresh_freshness(
        freshness_result.to_dict(),
        freshness_path=freshness_path,
        model_path=args.signal_model,
        feature_names=model.feature_names,
        raw_path=raw_path,
        features_path=features_path,
    )
    print(f"wrote refresh artifacts to {output_dir}")
    if not freshness_result.allowed:
        print(f"refresh-data blocked: {', '.join(freshness_result.reasons)}", file=sys.stderr)
    return 0 if freshness_result.allowed else 1


def _validate_data(args: argparse.Namespace) -> int:
    records = read_records(args.dataset)
    result = validate_ohlcv_records(records)
    if result.valid:
        print(f"valid dataset: {result.row_count} rows, {len(result.symbols)} symbols")
        return 0
    for error in result.errors:
        print(error, file=sys.stderr)
    return 1


def _manifest(args: argparse.Namespace) -> int:
    records = read_records(args.dataset)
    validation = validate_ohlcv_records(records)
    if not validation.valid:
        for error in validation.errors:
            print(error, file=sys.stderr)
        return 1
    manifest = build_dataset_manifest(records, source=str(args.dataset))
    manifest["dataset_path"] = str(Path(args.dataset))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote manifest to {output}")
    return 0


def _build_features(args: argparse.Namespace) -> int:
    records = read_records(args.dataset)
    validation = validate_ohlcv_records(records)
    if not validation.valid:
        for error in validation.errors:
            print(error, file=sys.stderr)
        return 1
    features = build_features(records)
    write_records(features, args.output)
    print(f"wrote {len(features)} feature rows to {args.output}")
    return 0


def _backtest(args: argparse.Namespace) -> int:
    if args.strategy != "momentum-vol-target":
        print(f"unknown strategy: {args.strategy}", file=sys.stderr)
        return 2
    risk = load_risk_config(args.config, allow_live=False)
    records = read_records(args.dataset)
    validation = validate_ohlcv_records(records)
    if not validation.valid:
        for error in validation.errors:
            print(error, file=sys.stderr)
        return 1
    result = run_momentum_vol_target_backtest(
        records,
        BacktestConfig(
            max_gross_exposure=risk.max_gross_exposure,
            max_single_position=risk.max_single_position,
        ),
    )
    metadata = build_dataset_manifest(records, source=str(args.dataset))
    metadata["dataset_path"] = str(Path(args.dataset))
    result = _with_metadata(result, metadata)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    report = Path(args.report_output)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_backtest_report(result), encoding="utf-8")
    print(f"wrote backtest to {output}")
    return 0


def _report(args: argparse.Namespace) -> int:
    run_path = Path(args.run_id)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    from trading_ai.backtest.engine import BacktestResult

    result = BacktestResult(
        config=BacktestConfig(**payload["config"]),
        daily_returns=tuple(payload["daily_returns"]),
        equity_curve=tuple(payload["equity_curve"]),
        positions=tuple(),
        trades=tuple(),
        metrics={key: float(value) for key, value in payload["metrics"].items()},
        metadata=payload.get("metadata", {}),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_backtest_report(result), encoding="utf-8")
    print(f"wrote report to {output}")
    return 0


def _drift_report(args: argparse.Namespace) -> int:
    reference_rows = read_records(args.reference_features)
    current_rows = read_records(args.current_features)
    report = evaluate_feature_drift(
        reference_rows,
        current_rows,
        feature_names=_parse_feature_names(args.feature_names),
        mean_z_threshold=args.mean_z_threshold,
        missing_delta_threshold=args.missing_delta_threshold,
        std_ratio_threshold=args.std_ratio_threshold,
        min_samples=args.min_samples,
        sources={
            "reference_features": str(Path(args.reference_features)),
            "current_features": str(Path(args.current_features)),
        },
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    markdown_output = Path(args.markdown_output)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.write_text(render_feature_drift_markdown(report), encoding="utf-8")
    print(f"wrote drift report to {output}")
    print(f"wrote drift report markdown to {markdown_output}")
    return 0


def _paper(args: argparse.Namespace) -> int:
    if args.broker != "alpaca":
        _append_paper_operational_error(args, "unknown_broker")
        print(f"unknown broker: {args.broker}", file=sys.stderr)
        return 2
    if args.real_paper and not args.confirm_paper:
        _append_paper_operational_error(args, "missing_confirm_paper")
        print("--real-paper requires --confirm-paper", file=sys.stderr)
        return 2
    if args.real_paper and args.kill_switch_test:
        _append_paper_operational_error(args, "kill_switch_real_paper_not_allowed")
        print("--kill-switch-test is local dry-run only; omit --real-paper", file=sys.stderr)
        return 2
    if args.cancel_order and not args.confirm_cancel:
        _append_paper_operational_error(args, "missing_confirm_cancel")
        print("--cancel-order requires --confirm-cancel", file=sys.stderr)
        return 2
    if (args.get_order or args.cancel_order) and not (args.order_id or args.client_order_id):
        _append_paper_operational_error(args, "missing_order_identifier")
        print("--get-order/--cancel-order requires --order-id or --client-order-id", file=sys.stderr)
        return 2
    if args.order_id and args.client_order_id:
        _append_paper_operational_error(args, "multiple_order_identifiers")
        print("provide only one of --order-id or --client-order-id", file=sys.stderr)
        return 2
    if args.reconcile_order and not args.source_report:
        _append_paper_operational_error(args, "missing_source_report")
        print("--reconcile-order requires --source-report", file=sys.stderr)
        return 2
    universe = load_universe_config(args.universe)
    risk = load_risk_config(args.risk, allow_live=False)
    dry_run = not args.real_paper
    client = None if dry_run else build_alpaca_paper_client()
    broker = AlpacaPaperBroker(client=client, allowlist=universe.symbols, risk_limits=risk, dry_run=dry_run)
    if args.kill_switch_test:
        broker.activate_kill_switch("cli_kill_switch_test")
        order_result = broker.submit_order(
            PaperOrder(
                symbol=universe.symbols[0],
                side="buy",
                quantity=1,
                client_order_id="kill-switch-test",
            )
        )
        cancel_result = broker.cancel_order("kill-switch-test")
        payload = {
            "mode": "dry-run",
            "broker": "alpaca",
            "kill_switch_active": True,
            "order_result": _paper_order_result_to_dict(order_result),
            "cancel_result": _paper_order_result_to_dict(cancel_result),
        }
        _write_json_output(payload, args.output)
        print(f"wrote paper kill-switch test to {args.output}")
        return 0
    if args.list_orders:
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "order_status": args.order_status,
            "orders": [_paper_order_snapshot_to_dict(order) for order in broker.list_orders(status=args.order_status)],
        }
        _write_json_output(payload, args.output)
        _append_paper_order_ledger(
            args,
            event_type="paper_order_list",
            payload=payload,
            exit_code=0,
        )
        print(f"wrote paper orders to {args.output}")
        return 0
    if args.get_order:
        order = _get_requested_order(broker, order_id=args.order_id, client_order_id=args.client_order_id)
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "order": _paper_order_snapshot_to_dict(order),
        }
        _write_json_output(payload, args.output)
        _append_paper_order_ledger(
            args,
            event_type="paper_order_query",
            payload=payload,
            exit_code=0,
        )
        print(f"wrote paper order to {args.output}")
        return 0
    if args.cancel_order:
        resolved_order = None
        if args.client_order_id:
            resolved_order = broker.get_order_by_client_id(args.client_order_id) if not dry_run else None
            cancel_result = broker.cancel_order(client_order_id=args.client_order_id)
        else:
            resolved_order = broker.get_order(order_id=args.order_id) if not dry_run else None
            cancel_result = broker.cancel_order(order_id=args.order_id)
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "resolved_order": _paper_order_snapshot_to_dict(resolved_order) if resolved_order is not None else None,
            "cancel_result": _paper_order_result_to_dict(cancel_result),
        }
        _write_json_output(payload, args.output)
        exit_code = 0 if cancel_result.accepted else 1
        _append_paper_order_ledger(
            args,
            event_type="paper_cancel_order",
            payload=payload,
            exit_code=exit_code,
        )
        print(f"wrote paper cancel report to {args.output}")
        return exit_code
    if args.reconcile_order:
        source_report = json.loads(Path(args.source_report).read_text(encoding="utf-8"))
        expected_order = source_report.get("order_intent") or {}
        client_order_id = str(expected_order.get("client_order_id", ""))
        if not client_order_id:
            _append_paper_operational_error(args, "missing_client_order_id_in_source_report")
            print("source report does not contain order_intent.client_order_id", file=sys.stderr)
            return 2
        current_order = broker.get_order_by_client_id(client_order_id) if not dry_run else None
        account = broker.read_account()
        positions = broker.read_positions()
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "expected_order": expected_order,
            "current_order": _paper_order_snapshot_to_dict(current_order) if current_order is not None else None,
            "account": _paper_account_to_dict(account),
            "positions": [_paper_position_to_dict(position) for position in positions],
            "reconciliation": _reconcile_order(expected_order, current_order, positions),
        }
        _write_json_output(payload, args.output)
        _append_paper_order_ledger(
            args,
            event_type="paper_reconciliation",
            payload=payload,
            exit_code=0,
            source_path=args.source_report,
        )
        print(f"wrote paper order reconciliation to {args.output}")
        return 0
    if args.submit_signal_order:
        model = load_model(args.signal_model)
        feature_rows = read_records(args.features)
        signals = generate_model_signals(
            feature_rows,
            model=model,
            allowlist=universe.symbols,
            threshold=args.signal_threshold,
        )
        selected_signal = _select_signal_to_submit(signals)
        signal_order_intent = None
        signal_order_result: PaperOrderResult | None = None
        submitted = False
        signal_order: PaperOrder | None = None
        signal_client_order_id: str | None = None
        if selected_signal is not None:
            signal_client_order_id = _signal_client_order_id(selected_signal)
            signal_order = PaperOrder(
                symbol=selected_signal.symbol,
                side="buy",
                notional=risk.paper_notional_usd,
                client_order_id=signal_client_order_id,
            )
            signal_order_intent = _paper_order_intent_to_dict(signal_order)
        open_orders = broker.list_orders(status="open")
        positions = broker.read_positions()
        preflight = evaluate_paper_preflight(
            signal=selected_signal,
            client_order_id=signal_client_order_id,
            open_orders=open_orders,
            positions=positions,
            as_of_date=_parse_cli_date(args.as_of_date) if args.as_of_date else date.today(),
            max_feature_age_days=args.max_feature_age_days,
        )
        if signal_order is not None and preflight.allowed:
            signal_order_result = broker.submit_order(signal_order)
            submitted = signal_order_result.accepted
        payload = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
            "preflight": _paper_preflight_to_dict(preflight),
            "open_orders": [_paper_order_snapshot_to_dict(order) for order in open_orders],
            "positions": [_paper_position_to_dict(position) for position in positions],
            "submitted": submitted,
            "signals": [_model_signal_to_dict(signal) for signal in signals],
            "selected_signal": _model_signal_to_dict(selected_signal) if selected_signal is not None else None,
            "order_intent": signal_order_intent,
            "order_result": (
                _paper_order_result_to_dict(signal_order_result) if signal_order_result is not None else None
            ),
            "account": _paper_account_to_dict(broker.read_account()),
        }
        _write_json_output(payload, args.output)
        print(f"wrote paper signal order report to {args.output}")
        return 0 if signal_order_result is None or signal_order_result.accepted else 1
    if args.read_account or args.read_positions:
        status_payload: dict[str, object] = {
            "mode": "dry-run" if dry_run else "real-paper",
            "broker": "alpaca",
        }
        if args.read_account:
            status_payload["account"] = _paper_account_to_dict(broker.read_account())
        if args.read_positions:
            status_payload["positions"] = [_paper_position_to_dict(position) for position in broker.read_positions()]
        _write_json_output(status_payload, args.output)
        print(f"wrote paper status to {args.output}")
        return 0
    mode = "dry-run" if dry_run else "real-paper"
    print(f"alpaca paper broker initialized in {mode} mode")
    return 0


def _paper_audit(args: argparse.Namespace) -> int:
    freshness_report = _read_json_report(args.freshness_report)
    signal_report = _read_json_report(args.signal_report)
    reconciliation_report = _read_optional_json_report(args.reconciliation_report)
    backtest_report = _read_optional_json_report(args.backtest_report)
    promotion_report = _read_optional_json_report(args.promotion_report)
    drift_report = _read_optional_json_report(args.drift_report)
    mlflow_candidate_review_report = _read_optional_mlflow_candidate_review_report(args.mlflow_candidate_review_report)
    paper_graduation_report = load_optional_json_report(args.paper_graduation_report)
    as_of_date = _resolve_as_of_date(args.as_of_date)
    sources = {
        "freshness_report": str(Path(args.freshness_report)),
        "signal_report": str(Path(args.signal_report)),
    }
    if args.reconciliation_report:
        sources["reconciliation_report"] = str(Path(args.reconciliation_report))
    if args.backtest_report:
        sources["backtest_report"] = str(Path(args.backtest_report))
    if args.promotion_report:
        sources["promotion_report"] = str(Path(args.promotion_report))
    if args.drift_report:
        sources["drift_report"] = str(Path(args.drift_report))
    if args.mlflow_candidate_review_report:
        sources["mlflow_candidate_review_report"] = str(Path(args.mlflow_candidate_review_report))
    if args.paper_graduation_report:
        sources["paper_graduation_report"] = str(Path(args.paper_graduation_report))

    report = evaluate_paper_audit(
        freshness_report=freshness_report,
        signal_report=signal_report,
        reconciliation_report=reconciliation_report,
        backtest_report=backtest_report,
        promotion_report=promotion_report,
        drift_report=drift_report,
        mlflow_candidate_review_report=mlflow_candidate_review_report,
        paper_graduation_report=paper_graduation_report,
        sources=sources,
        as_of_date=as_of_date.isoformat(),
    )
    output = Path(args.output)
    write_json_artifact(report.to_dict(), output)
    markdown_output = Path(args.markdown_output)
    write_text_artifact(
        render_paper_audit_markdown(report, freshness_report=freshness_report, signal_report=signal_report),
        markdown_output,
    )
    print(f"wrote paper audit to {output}")
    print(f"wrote paper audit markdown to {markdown_output}")
    return 0 if report.ready_for_paper_review else 1


def _paper_session(args: argparse.Namespace) -> int:
    try:
        result = run_offline_paper_session(
            source_csv=args.source_csv,
            start=args.start,
            end=args.end,
            reference_features=args.reference_features,
            output_dir=args.output_dir,
            config=args.config,
            risk=args.risk,
            signal_model=args.signal_model,
            as_of_date=args.as_of_date,
            signal_threshold=args.signal_threshold,
            max_age_days=args.max_age_days,
            max_feature_age_days=args.max_feature_age_days,
            backtest_report=args.backtest_report,
            promotion_report=args.promotion_report,
            reconciliation_report=args.reconciliation_report,
            campaign_report=args.campaign_report,
            phase_review=args.phase_review,
            review_mlflow_paper_candidate=args.review_mlflow_paper_candidate,
            mlflow_registry_dir=args.mlflow_registry_dir,
            mlflow_tracking_uri=args.mlflow_tracking_uri,
            mlflow_registered_model_name=args.mlflow_registered_model_name,
            mlflow_alias=args.mlflow_alias,
        )
    except Exception as exc:
        append_paper_ledger_event(
            args.ledger_output,
            paper_session_ledger_event(
                session_dir=args.output_dir,
                exit_code=2,
                source_path=args.source_csv,
                reasons=[str(exc)],
            ),
        )
        print(f"error: {exc}", file=sys.stderr)
        return 2
    append_paper_ledger_event(
        args.ledger_output,
        paper_session_ledger_event(
            session_dir=result.output_dir,
            exit_code=result.exit_code,
            source_path=args.source_csv,
        ),
    )
    print(f"wrote paper session to {result.session_path}")
    print(f"wrote paper audit to {result.audit_path}")
    return result.exit_code


def _paper_execute_session(args: argparse.Namespace) -> int:
    try:
        result = run_paper_execute_session(
            session_dir=args.session_dir,
            confirm_paper=args.confirm_paper,
            confirm_submit=args.confirm_submit,
            confirm_dynamic_position_actions=args.confirm_dynamic_position_actions,
            output_dir=args.output_dir,
            as_of_date=args.as_of_date,
            max_feature_age_days=args.max_feature_age_days,
            risk_state_path=args.risk_state_path,
        )
    except PaperExecuteOperationalError as exc:
        append_paper_ledger_event(
            args.ledger_output,
            paper_execution_ledger_event(
                session_dir=args.session_dir,
                exit_code=2,
                status="ERROR",
                reasons=[str(exc)],
            ),
        )
        print(str(exc), file=sys.stderr)
        return 2
    append_paper_ledger_event(
        args.ledger_output,
        paper_execution_ledger_event(
            session_dir=args.session_dir,
            exit_code=result.exit_code,
            execution_path=result.json_path,
            status=result.status,
            reasons=result.reasons,
        ),
    )
    if result.json_path is not None:
        print(f"wrote paper execution to {result.json_path}")
    if result.markdown_path is not None:
        print(f"wrote paper execution markdown to {result.markdown_path}")
    for reason in result.reasons:
        print(reason, file=sys.stderr)
    return result.exit_code


def _paper_position_watch(args: argparse.Namespace) -> int:
    try:
        result = run_paper_position_watch(
            session_dir=args.session_dir,
            confirm_paper=args.confirm_paper,
            confirm_dynamic_position_actions=args.confirm_dynamic_position_actions,
            as_of_date=args.as_of_date,
            output=args.output,
            markdown_output=args.markdown_output,
        )
    except PaperPositionWatchOperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper position watch to {result.output_path}")
    print(f"wrote paper position watch markdown to {result.markdown_path}")
    return result.exit_code


def _paper_safe_flatten(args: argparse.Namespace) -> int:
    try:
        result = run_paper_safe_flatten(
            confirm_paper=args.confirm_paper,
            confirm_flatten=args.confirm_flatten,
            config=args.universe,
            risk=args.risk,
            reset_kill_switch_after=args.reset_kill_switch_after,
            as_of_date=args.as_of_date,
            risk_state_path=args.risk_state_path,
            output=args.output,
            markdown_output=args.markdown_output,
        )
    except PaperSafeFlattenOperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper safe flatten to {result.output_path}")
    print(f"wrote paper safe flatten markdown to {result.markdown_path}")
    return result.exit_code


def _paper_close_session(args: argparse.Namespace) -> int:
    try:
        result = run_paper_close_session(
            session_dir=args.session_dir,
            confirm_paper=args.confirm_paper,
            execution_report=args.execution_report,
            output_dir=args.output_dir,
        )
    except PaperCloseOperationalError as exc:
        append_paper_ledger_event(
            args.ledger_output,
            paper_closeout_ledger_event(
                session_dir=args.session_dir,
                exit_code=2,
                status="ERROR",
                reasons=[str(exc)],
            ),
        )
        print(str(exc), file=sys.stderr)
        return 2
    append_paper_ledger_event(
        args.ledger_output,
        paper_closeout_ledger_event(
            session_dir=result.session_dir,
            exit_code=result.exit_code,
            closeout_path=result.json_path,
            status=result.status,
            client_order_id=result.client_order_id,
            symbol=result.symbol,
            side=result.side,
            notional=result.notional,
            reasons=result.reasons,
        ),
    )
    if result.json_path is not None:
        print(f"wrote paper closeout to {result.json_path}")
    if result.markdown_path is not None:
        print(f"wrote paper closeout markdown to {result.markdown_path}")
    for reason in result.reasons:
        print(reason, file=sys.stderr)
    return result.exit_code


def _paper_observability(args: argparse.Namespace) -> int:
    report = build_paper_observability_report(
        sessions_root=args.sessions_root,
        session_dirs=args.session_dir,
        ledger_inputs=args.ledger_input,
    )
    write_paper_observability_report(
        report,
        output=args.output,
        markdown_output=args.markdown_output,
    )
    print(f"wrote paper observability to {args.output}")
    print(f"wrote paper observability markdown to {args.markdown_output}")
    return 0


def _paper_monitor(args: argparse.Namespace) -> int:
    if args.min_stable_sessions < 1:
        print("--min-stable-sessions must be at least 1", file=sys.stderr)
        return 2
    if args.broker_read_only and not args.confirm_paper:
        print("--broker-read-only requires --confirm-paper", file=sys.stderr)
        return 2
    try:
        result = run_paper_monitor(
            sessions_root=args.sessions_root,
            session_dirs=args.session_dir,
            ledger_inputs=args.ledger_input,
            output=args.output,
            markdown_output=args.markdown_output,
            as_of_date=args.as_of_date,
            min_stable_sessions=args.min_stable_sessions,
            broker_read_only=args.broker_read_only,
            confirm_paper=args.confirm_paper,
            universe=args.universe,
            risk=args.risk,
            order_status=args.order_status,
            ledger_output=args.ledger_output,
            send_telegram=args.send_telegram,
            telegram_dry_run=args.telegram_dry_run,
            telegram_send_warnings=args.telegram_send_warnings,
        )
    except (PaperMonitorOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper monitor to {result.output_path}")
    print(f"wrote paper monitor markdown to {result.markdown_path}")
    if result.status == "CRITICAL":
        print("paper monitor critical alerts present", file=sys.stderr)
    return result.exit_code


def _paper_campaign_report(args: argparse.Namespace) -> int:
    try:
        report = build_paper_campaign_report(
            sessions_root=args.sessions_root,
            readiness_root=args.readiness_root,
            decisions_root=args.decisions_root,
            performance_root=args.performance_root,
            trial_day_root=args.trial_day_root,
            ledger_inputs=args.ledger_input,
            min_paper_auto_clean_sessions=args.min_paper_auto_clean_sessions,
            min_stable_sessions=args.min_stable_sessions,
            min_trial_days=args.min_trial_days,
            risk=args.risk,
            as_of_date=args.as_of_date,
        )
        result = write_paper_campaign_report(
            report,
            output=args.output,
            markdown_output=args.markdown_output,
        )
    except (PaperCampaignOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper campaign report to {result.output_path}")
    print(f"wrote paper campaign report markdown to {result.markdown_path}")
    if result.status == "CRITICAL":
        print("paper campaign critical blockers present", file=sys.stderr)
    return result.exit_code


def _paper_day_close(args: argparse.Namespace) -> int:
    try:
        result = run_paper_day_close(
            readiness=args.readiness,
            broker_run=args.broker_run,
            monitor=args.monitor,
            campaign_report=args.campaign_report,
            output_dir=args.output_dir,
            as_of_date=args.as_of_date,
            operator=args.operator,
            reason=args.reason,
            ledger_output=args.ledger_output,
        )
    except (PaperDayCloseOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper day close decision to {result.output_path}")
    print(f"wrote paper day close markdown to {result.markdown_path}")
    if result.decision in {"STOP", "ERROR"}:
        print(f"paper day close {result.decision.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_performance_report(args: argparse.Namespace) -> int:
    try:
        result = run_paper_performance_report(
            sessions_root=args.sessions_root,
            session_dirs=args.session_dir,
            ledger_inputs=args.ledger_input,
            backtest_report=args.backtest_report,
            broker_statement=args.broker_statement,
            min_stable_sessions=args.min_stable_sessions,
            min_stable_fills=args.min_stable_fills,
            output=args.output,
            markdown_output=args.markdown_output,
        )
    except (PaperPerformanceOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper performance report to {result.output_path}")
    print(f"wrote paper performance markdown to {result.markdown_path}")
    return result.exit_code


def _paper_statement_validate(args: argparse.Namespace) -> int:
    try:
        result = run_paper_statement_validate(
            statement=args.statement,
            as_of_date=args.as_of_date,
            output_dir=args.output_dir,
        )
    except (PaperStatementOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote normalized paper statement to {result.output_path}")
    print(f"wrote normalized paper statement markdown to {result.markdown_path}")
    if result.status == "ERROR":
        print("paper statement validation error", file=sys.stderr)
    return result.exit_code


def _paper_weekly_summary(args: argparse.Namespace) -> int:
    try:
        result = run_paper_weekly_summary(
            decisions_root=args.decisions_root,
            performance_root=args.performance_root,
            campaign_root=args.campaign_root,
            ledger_inputs=args.ledger_input,
            output_dir=args.output_dir,
            week=args.week,
            as_of_date=args.as_of_date,
            history_weeks=args.history_weeks,
        )
    except (PaperWeeklySummaryOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper weekly summary to {result.output_path}")
    print(f"wrote paper weekly summary markdown to {result.markdown_path}")
    if result.status in {"CRITICAL", "ERROR"}:
        print(f"paper weekly summary {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_operator_status(args: argparse.Namespace) -> int:
    try:
        result = run_paper_operator_status(
            as_of_date=args.as_of_date,
            cycle_root=args.cycle_root,
            ledger=args.ledger,
            monitor=args.monitor,
            performance=args.performance,
            lock_dir=args.lock_dir,
            max_lock_age_minutes=args.max_lock_age_minutes,
            output_dir=args.output_dir,
        )
    except (PaperOperatorStatusOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper operator status to {result.output_path}")
    print(f"wrote paper operator status markdown to {result.markdown_path}")
    if result.status in {"CRITICAL", "ERROR"}:
        print(f"paper operator status {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_strategy_quality(args: argparse.Namespace) -> int:
    try:
        result = run_paper_strategy_quality(
            as_of_date=args.as_of_date,
            model_signals=args.model_signals,
            signal_plan=args.signal_plan,
            performance=args.performance,
            challenger_report=args.challenger_report,
            ledger_inputs=args.ledger_input,
            lookback_sessions=args.lookback_sessions,
            min_clean_sessions=args.min_clean_sessions,
            min_paper_fills=args.min_paper_fills,
            max_cost_drag_bps=args.max_cost_drag_bps,
            max_trade_count_gap_pct=args.max_trade_count_gap_pct,
            max_blocker_rate_pct=args.max_blocker_rate_pct,
            max_llm_disagreement_rate_pct=args.max_llm_disagreement_rate_pct,
            output_dir=args.output_dir,
        )
    except (PaperStrategyQualityOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper strategy quality to {result.output_path}")
    print(f"wrote paper strategy quality markdown to {result.markdown_path}")
    if result.status == "ERROR":
        print("paper strategy quality error", file=sys.stderr)
    return result.exit_code


def _paper_phase_review_report(args: argparse.Namespace) -> int:
    try:
        result = run_paper_phase_review_report(
            as_of_date=args.as_of_date,
            campaign_report=args.campaign_report,
            performance_report=args.performance_report,
            operator_status=args.operator_status,
            strategy_quality=args.strategy_quality,
            evidence_index=args.evidence_index,
            risk=args.risk,
            weekly_summary=args.weekly_summary,
            trial_day_root=args.trial_day_root,
            min_stable_sessions=args.min_stable_sessions,
            output_dir=args.output_dir,
        )
    except (PaperPhaseReviewOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper phase review to {result.output_path}")
    print(f"wrote paper phase review markdown to {result.markdown_path}")
    if result.status in {"CRITICAL", "ERROR"}:
        print(f"paper phase review {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_ops_check(args: argparse.Namespace) -> int:
    try:
        result = run_paper_ops_check(
            as_of_date=args.as_of_date,
            readiness_root=args.readiness_root,
            sessions_root=args.sessions_root,
            monitor_root=args.monitor_root,
            campaign_root=args.campaign_root,
            decisions_root=args.decisions_root,
            performance_root=args.performance_root,
            ledger_inputs=args.ledger_input,
            output_dir=args.output_dir,
        )
    except (PaperOpsCheckOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper ops check to {result.output_path}")
    print(f"wrote paper ops check markdown to {result.markdown_path}")
    if result.status in {"CRITICAL", "ERROR"}:
        print(f"paper ops check {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_ops_rehearsal(args: argparse.Namespace) -> int:
    try:
        result = run_paper_ops_rehearsal(
            as_of_date=args.as_of_date,
            scenario=args.scenario,
            output_dir=args.output_dir,
        )
    except (PaperOpsRehearsalOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper ops rehearsal to {result.output_path}")
    print(f"wrote paper ops rehearsal markdown to {result.markdown_path}")
    if result.status in {"CRITICAL", "ERROR"}:
        print(f"paper ops rehearsal {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_evidence_index(args: argparse.Namespace) -> int:
    try:
        result = run_paper_evidence_index(
            as_of_date=args.as_of_date,
            readiness_root=args.readiness_root,
            monitor_root=args.monitor_root,
            campaign_root=args.campaign_root,
            decisions_root=args.decisions_root,
            performance_root=args.performance_root,
            ops_root=args.ops_root,
            weekly_root=args.weekly_root,
            statement_root=args.statement_root,
            challenger_decisions_root=args.challenger_decisions_root,
            output_dir=args.output_dir,
        )
    except (PaperEvidenceIndexOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper evidence index to {result.output_path}")
    print(f"wrote paper evidence index markdown to {result.markdown_path}")
    if result.status == "ERROR":
        print("paper evidence index error", file=sys.stderr)
    return result.exit_code


def _llm_paper_review(args: argparse.Namespace) -> int:
    try:
        result = run_llm_paper_review(
            as_of_date=args.as_of_date,
            readiness=args.readiness,
            ops_check=args.ops_check,
            evidence_index=args.evidence_index,
            performance=args.performance,
            challenger_report=args.challenger_report,
            shadow_scorecard=args.shadow_scorecard,
            paper_model_alias=args.paper_model_alias,
            llm_model_alias=args.llm_model_alias,
            cycle_report=args.cycle_report,
            output_dir=args.output_dir,
            use_openai=args.use_openai,
            confirm_llm=args.confirm_llm,
            model=args.model,
        )
    except (LlmPaperReviewOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM paper review to {result.output_path}")
    print(f"wrote LLM paper review markdown to {result.markdown_path}")
    if result.status in {"BLOCKED", "ERROR"}:
        print(f"LLM paper review {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _llm_signal_proposals(args: argparse.Namespace) -> int:
    try:
        result = run_llm_signal_proposals(
            as_of_date=args.as_of_date,
            readiness=args.readiness,
            features=args.features,
            model_signals=args.model_signals,
            output_dir=args.output_dir,
            use_openai=args.use_openai,
            confirm_llm=args.confirm_llm,
            context_digest=args.context_digest,
            llm_model_alias=args.llm_model_alias,
            model=args.model,
        )
    except (LLMSignalProposalsOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM signal proposals to {result.output_path}")
    print(f"wrote LLM signal proposals markdown to {result.markdown_path}")
    if result.status in {"BLOCKED", "ERROR"}:
        print(f"LLM signal proposals {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _llm_context_pack(args: argparse.Namespace) -> int:
    try:
        result = run_llm_context_pack(
            as_of_date=args.as_of_date,
            cycle_root=args.cycle_root,
            campaign_status=args.campaign_status,
            performance_report=args.performance_report,
            phase_review=args.phase_review,
            training_cycle=args.training_cycle,
            challenger_report=args.challenger_report,
            shadow_plan=args.shadow_plan,
            shadow_scorecard=args.shadow_scorecard,
            paper_model_alias=args.paper_model_alias,
            llm_model_alias=args.llm_model_alias,
            evidence_index=args.evidence_index,
            weekly_summary=args.weekly_summary,
            operator_status=args.operator_status,
            quality_report=args.quality_report,
            output_dir=args.output_dir,
        )
    except (LlmContextPackOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM context pack to {result.output_path}")
    print(f"wrote LLM context pack markdown to {result.markdown_path}")
    if result.status in {"BLOCKED", "ERROR"}:
        print(f"LLM context pack {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_signal_arbitration(args: argparse.Namespace) -> int:
    try:
        result = run_paper_signal_arbitration(
            as_of_date=args.as_of_date,
            model_signals=args.model_signals,
            llm_proposals=args.llm_proposals,
            readiness=args.readiness,
            features=args.features,
            shadow_plan=args.shadow_plan,
            challenger_signals=args.challenger_signals,
            output_dir=args.output_dir,
        )
    except (PaperSignalArbitrationOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper signal arbitration to {result.output_path}")
    print(f"wrote paper signal arbitration markdown to {result.markdown_path}")
    if result.decision == "BLOCKED":
        print("paper signal arbitration blocked", file=sys.stderr)
    return result.exit_code


def _paper_challenger_signals(args: argparse.Namespace) -> int:
    try:
        result = run_paper_challenger_signals(
            as_of_date=args.as_of_date,
            model_run=args.model_run,
            features=args.features,
            readiness=args.readiness,
            output_dir=args.output_dir,
        )
    except (PaperChallengerSignalsOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper challenger signals to {result.output_path}")
    print(f"wrote paper challenger signals markdown to {result.markdown_path}")
    if result.status == "BLOCKED":
        print("paper challenger signals blocked", file=sys.stderr)
    return result.exit_code


def _paper_shadow_outcome_report(args: argparse.Namespace) -> int:
    result = run_paper_shadow_outcome_report(
        as_of_date=args.as_of_date,
        signal_plan=args.signal_plan,
        approved_dir=args.approved_dir,
        ledger_output=args.ledger_output,
        horizon_days=args.horizon_days,
        output_dir=args.output_dir,
    )
    print(f"wrote paper shadow outcome to {result.output_path}")
    print(f"wrote paper shadow outcome markdown to {result.markdown_path}")
    if result.state == "BLOCKED":
        print("paper shadow outcome blocked", file=sys.stderr)
    return result.exit_code


def _paper_shadow_scorecard(args: argparse.Namespace) -> int:
    result = run_paper_shadow_scorecard(
        ledger_input=args.ledger_input,
        phase_review=args.phase_review,
        paper_performance=args.paper_performance,
        min_shadow_trades=args.min_shadow_trades,
        min_win_rate=args.min_win_rate,
        min_avg_forward_return_bps=args.min_avg_forward_return_bps,
        max_shadow_drawdown_pct=args.max_shadow_drawdown_pct,
        max_missing_outcome_rate_pct=args.max_missing_outcome_rate_pct,
        output_dir=args.output_dir,
    )
    print(f"wrote paper shadow scorecard to {result.output_path}")
    print(f"wrote paper shadow scorecard markdown to {result.markdown_path}")
    return result.exit_code


def _paper_trial_day(args: argparse.Namespace) -> int:
    result = run_paper_trial_day(
        as_of_date=args.as_of_date,
        cycle=args.cycle,
        monitor=args.monitor,
        performance=args.performance,
        shadow_outcome=args.shadow_outcome,
        risk=args.risk,
        output_dir=args.output_dir,
    )
    print(f"wrote paper trial day to {result.output_path}")
    print(f"wrote paper trial day markdown to {result.markdown_path}")
    if result.trial_state in {"RECOVERY_REQUIRED", "ERROR"}:
        print(f"paper trial day {result.trial_state.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_model_alias_decision(args: argparse.Namespace) -> int:
    result = run_paper_model_alias_decision(
        shadow_scorecard=args.shadow_scorecard,
        review_decision=args.review_decision,
        candidate_model_run=args.candidate_model_run,
        latest_model=args.latest_model,
        reviewer=args.reviewer,
        reason=args.reason,
        ttl_days=args.ttl_days,
        output_dir=args.output_dir,
    )
    print(f"wrote paper model alias to {result.output_path}")
    print(f"wrote paper model alias markdown to {result.markdown_path}")
    return result.exit_code


def _paper_challenger_shadow_plan(args: argparse.Namespace) -> int:
    try:
        result = run_paper_challenger_shadow_plan(
            challenger_report=args.challenger_report,
            review_decision=args.review_decision,
            latest_model=args.latest_model,
            approved_manifest=args.approved_manifest,
            feature_schema=args.feature_schema,
            output_dir=args.output_dir,
        )
    except (PaperChallengerShadowOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper challenger shadow plan to {result.output_path}")
    print(f"wrote paper challenger shadow markdown to {result.markdown_path}")
    if result.shadow_state == "BLOCKED":
        print("paper challenger shadow plan blocked", file=sys.stderr)
    return result.exit_code


def _paper_autopilot_plan(args: argparse.Namespace) -> int:
    try:
        result = run_paper_autopilot_plan(
            as_of_date=args.as_of_date,
            readiness=args.readiness,
            ops_check=args.ops_check,
            evidence_index=args.evidence_index,
            llm_review=args.llm_review,
            human_review=args.human_review,
            permissions=args.permissions,
            output_dir=args.output_dir,
        )
    except (PaperAutopilotPlanOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper autopilot plan to {result.output_path}")
    print(f"wrote paper autopilot markdown to {result.markdown_path}")
    if result.status in {"BLOCKED", "ERROR"}:
        print(f"paper autopilot {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_review_decision(args: argparse.Namespace) -> int:
    try:
        result = run_paper_review_decision(
            as_of_date=args.as_of_date,
            decision=args.decision,
            reviewer=args.reviewer,
            reason=args.reason,
            output_dir=args.output_dir,
        )
    except (PaperReviewDecisionOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper review decision to {result.output_path}")
    print(f"wrote paper review decision markdown to {result.markdown_path}")
    if result.status == "ERROR":
        print("paper review decision error", file=sys.stderr)
    return result.exit_code


def _paper_bot_cycle(args: argparse.Namespace) -> int:
    try:
        result = run_paper_bot_cycle(
            as_of_date=args.as_of_date,
            readiness=args.readiness,
            human_review=args.human_review,
            llm_review=args.llm_review,
            ops_check=args.ops_check,
            evidence_index=args.evidence_index,
            signal_plan=args.signal_plan,
            permissions=args.permissions,
            output_dir=args.output_dir,
            confirm_readiness=args.confirm_readiness,
            confirm_paper=args.confirm_paper,
            confirm_auto_submit=args.confirm_auto_submit,
            confirm_auto_close=args.confirm_auto_close,
            require_clean_state=args.require_clean_state,
        )
    except (PaperBotCycleOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper bot cycle to {result.output_path}")
    print(f"wrote paper bot cycle markdown to {result.markdown_path}")
    if result.state == "BLOCKED":
        print("paper bot cycle blocked", file=sys.stderr)
    return result.exit_code


def _paper_auto_cycle(args: argparse.Namespace) -> int:
    try:
        result = run_paper_auto_cycle(
            as_of_date=args.as_of_date,
            source=args.source,
            approved_dir=args.approved_dir,
            dataset_id=args.dataset_id,
            frequency=args.frequency,
            start=args.start,
            end=args.end,
            output_dir=args.output_dir,
            confirm_paper_auto=args.confirm_paper_auto,
            provider=args.provider,
            license_note=args.license_note,
            config=args.config,
            risk=args.risk,
            signal_model=args.signal_model,
            paper_model_alias=args.paper_model_alias,
            approved_output_dir=args.approved_output_dir,
            registry_dir=args.registry_dir,
            use_openai=args.use_openai,
            confirm_llm=args.confirm_llm,
            monitor=args.monitor,
            performance=args.performance,
            operator_status=args.operator_status,
            campaign_report=args.campaign_report,
            lock_dir=args.lock_dir,
            session_ledger=args.session_ledger,
            require_clean_state=args.require_clean_state,
        )
    except (PaperAutoCycleOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper auto cycle to {result.output_path}")
    print(f"wrote paper auto cycle markdown to {result.markdown_path}")
    if result.state in {"BLOCKED", "ERROR"}:
        print(f"paper auto cycle {result.state.lower()}", file=sys.stderr)
    return result.exit_code


def _live_readiness_report(args: argparse.Namespace) -> int:
    result = run_live_readiness_report(
        as_of_date=args.as_of_date,
        phase_review=args.phase_review,
        campaign_report=args.campaign_report,
        performance_report=args.performance_report,
        permissions=args.permissions,
        reviewer=args.reviewer,
        reason=args.reason,
        output_dir=args.output_dir,
    )
    print(f"wrote live readiness report to {result.output_path}")
    print(f"wrote live readiness markdown to {result.markdown_path}")
    if result.state in {"BLOCKED", "ERROR"}:
        print(f"live readiness {result.state.lower()}", file=sys.stderr)
    return result.exit_code


def _live_execute_session(args: argparse.Namespace) -> int:
    try:
        result = run_live_execute_session(
            as_of_date=args.as_of_date,
            readiness=args.readiness,
            risk=args.risk,
            expected_readiness_hash=args.expected_readiness_hash,
            reviewer=args.reviewer,
            reason=args.reason,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
            command_evidence=["trading-ai live-execute-session --dry-run"],
        )
    except (OSError, ValueError, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote live execute session to {result.output_path}")
    print(f"wrote live execute session markdown to {result.markdown_path}")
    if result.status in {"BLOCKED", "ERROR"}:
        print(f"live execute session {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _live_canary(args: argparse.Namespace) -> int:
    try:
        result = run_live_canary(
            as_of_date=args.as_of_date,
            symbol=args.symbol,
            notional_usd=args.notional_usd,
            readiness=args.readiness,
            expected_readiness_hash=args.expected_readiness_hash,
            breaker_state_path=args.breaker_state,
            rehearsal_summary=args.rehearsal_summary,
            rollback_evidence=args.rollback_evidence,
            reviewer=args.reviewer,
            reason=args.reason,
            confirmation=args.confirmation,
            output_dir=args.output_dir,
            market_open=args.market_open_confirmed,
            enable_real_submit=args.enable_real_submit,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote live canary evidence to {result.output_path}")
    print(f"wrote live canary markdown to {result.markdown_path}")
    if result.status == "BLOCKED":
        print("live canary blocked", file=sys.stderr)
    return result.exit_code


def _live_rehearsal(args: argparse.Namespace) -> int:
    try:
        result = run_live_rehearsal(fixtures=args.fixtures, output=args.output)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote live rehearsal summary to {result.summary_path}")
    print(f"wrote live rehearsal markdown to {result.markdown_path}")
    print(f"wrote live rehearsal evidence index to {result.evidence_index_path}")
    if result.status == "FAILED":
        print("live rehearsal failed", file=sys.stderr)
    return result.exit_code


def _futures_readiness_report(args: argparse.Namespace) -> int:
    try:
        result = run_futures_readiness_report(
            config=args.config,
            output=args.output,
            markdown_output=args.markdown_output,
        )
    except (ConfigError, FuturesReadinessOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote futures readiness report to {result.output_path}")
    print(f"wrote futures readiness markdown to {result.markdown_path}")
    if result.status == "BLOCKED":
        print("futures readiness blocked", file=sys.stderr)
    return result.exit_code


def _futures_research_scaffold(args: argparse.Namespace) -> int:
    try:
        result = run_futures_research_scaffold(
            config=args.config,
            output_dir=args.output_dir,
            as_of_date=args.as_of_date,
        )
    except (ConfigError, FuturesResearchOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote futures research scaffold to {result.output_path}")
    print(f"wrote futures research scaffold markdown to {result.markdown_path}")
    if result.status == "BLOCKED":
        print("futures research scaffold blocked", file=sys.stderr)
    return result.exit_code


def _paper_daily(args: argparse.Namespace) -> int:
    try:
        config = load_paper_daily_config(
            args.config,
            source_csv=args.source_csv,
            start=args.start,
            end=args.end,
            as_of_date=args.as_of_date,
            session_dir=args.session_dir,
            sessions_root=args.sessions_root,
            ledger_output=args.ledger_output,
            output=args.output,
            markdown_output=args.markdown_output,
        )
        result = run_paper_daily(
            config=config,
            confirm_paper=args.confirm_paper,
            confirm_auto_close=args.confirm_auto_close,
            confirm_auto_submit=args.confirm_auto_submit,
            send_telegram=args.send_telegram,
            telegram_dry_run=args.telegram_dry_run,
            telegram_send_warnings=args.telegram_send_warnings,
        )
    except (ConfigError, PaperDailyOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper daily report to {result.output_path}")
    print(f"wrote paper daily markdown to {result.markdown_path}")
    if result.exit_code == 1:
        print(f"paper daily {result.status.lower()}", file=sys.stderr)
    elif result.exit_code == 2:
        print("paper daily operational error", file=sys.stderr)
    return result.exit_code


def _paper_daily_from_readiness(args: argparse.Namespace) -> int:
    try:
        result = run_paper_daily_from_readiness(
            readiness_path=args.readiness,
            confirm_readiness=args.confirm_readiness,
            confirm_paper=args.confirm_paper,
            confirm_auto_close=args.confirm_auto_close,
            confirm_auto_submit=args.confirm_auto_submit,
            require_clean_state=args.require_clean_state,
            output_dir=args.output_dir,
            ledger_output=args.ledger_output,
        )
    except (ConfigError, PaperDailyOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper daily broker-confirmed report to {result.output_path}")
    print(f"wrote paper daily broker-confirmed markdown to {result.markdown_path}")
    if result.exit_code == 1:
        print(f"paper-daily-from-readiness {result.status.lower()}", file=sys.stderr)
    elif result.exit_code == 2:
        print("paper-daily-from-readiness operational error", file=sys.stderr)
    return result.exit_code


def _prepare_paper_daily(args: argparse.Namespace) -> int:
    try:
        result = prepare_paper_daily(
            source=args.source,
            approved_dir=args.approved_dir,
            dataset_id=args.dataset_id,
            frequency=args.frequency,
            start=args.start,
            end=args.end,
            as_of_date=args.as_of_date,
            provider=args.provider,
            license_note=args.license_note,
            config=args.config,
            risk=args.risk,
            signal_model=args.signal_model,
            paper_model_alias=args.paper_model_alias,
            reference_features=args.reference_features,
            candidate_spec=args.candidate_spec,
            approved_output_dir=args.approved_output_dir,
            output_dir=args.output_dir,
            registry_dir=args.registry_dir,
            periods_per_year=args.periods_per_year,
            min_accuracy_lift=args.min_accuracy_lift,
            min_test_samples=args.min_test_samples,
            run_offline_smoke=args.run_offline_smoke,
        )
    except PaperDailyPrepareOperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper daily readiness to {result.readiness_path}")
    print(f"wrote paper daily readiness markdown to {result.readiness_markdown_path}")
    if result.paper_daily_config_path is not None:
        print(f"wrote generated paper daily config to {result.paper_daily_config_path}")
    if result.exit_code != 0:
        reasons = result.payload.get("reasons", [])
        if isinstance(reasons, list):
            for reason in reasons:
                print(str(reason), file=sys.stderr)
    if result.exit_code == 1:
        print(f"prepare-paper-daily {result.status.lower()}", file=sys.stderr)
    elif result.exit_code == 2:
        print("prepare-paper-daily operational error", file=sys.stderr)
    return result.exit_code


def _train(args: argparse.Namespace) -> int:
    if args.model != "logistic-baseline":
        print("only logistic-baseline is implemented without optional ML dependencies", file=sys.stderr)
        return 2
    records = read_records(args.dataset)
    manifest = build_dataset_manifest(records, source=str(args.dataset))
    feature_names = _default_feature_names(records)
    config = LogisticBaselineConfig(feature_names=feature_names)
    examples = build_supervised_examples(records, feature_names=config.feature_names)
    split = temporal_train_test_split(examples, test_fraction=config.test_fraction)
    model = train_logistic_baseline(split.train, config)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_model(model, str(output))
    run_payload = {
        "model_type": args.model,
        "model_path": str(output),
        "dataset_path": str(Path(args.dataset)),
        "dataset_hash": manifest["dataset_hash"],
        "feature_names": list(config.feature_names),
        "train_range": [split.train[0].timestamp, split.train[-1].timestamp],
        "test_range": [split.test[0].timestamp, split.test[-1].timestamp],
        "metrics": {
            "train": evaluate_classifier(model, split.train),
            "test": evaluate_classifier(model, split.test),
            "walk_forward": walk_forward_evaluate(
                examples,
                config,
                min_train_size=max(2, len(split.train) // 2),
                test_size=max(1, len(split.test)),
            ),
        },
    }
    run_output = Path(args.run_output)
    run_output.parent.mkdir(parents=True, exist_ok=True)
    run_output.write_text(json.dumps(run_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote model to {output}")
    print(f"wrote training run to {run_output}")
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    run_path = Path(args.run_id)
    run_payload = json.loads(run_path.read_text(encoding="utf-8"))
    records = read_records(run_payload["dataset_path"])
    feature_names = tuple(str(name) for name in run_payload["feature_names"])
    examples = build_supervised_examples(records, feature_names=feature_names)
    split = temporal_train_test_split(examples, test_fraction=0.25)
    model = load_model(run_payload["model_path"])
    eval_payload = {
        "run_id": str(run_path),
        "model_path": run_payload["model_path"],
        "dataset_hash": build_dataset_manifest(records, source=run_payload["dataset_path"])["dataset_hash"],
        "metrics": {
            "train": evaluate_classifier(model, split.train),
            "test": evaluate_classifier(model, split.test),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(eval_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote evaluation to {output}")
    return 0


def _promote(args: argparse.Namespace) -> int:
    run_payload = json.loads(Path(args.run_id).read_text(encoding="utf-8"))
    baseline_payload = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    challenger_metrics = run_payload.get("metrics", {}).get("test", {})
    decision = evaluate_promotion(
        challenger_metrics=challenger_metrics,
        baseline_metrics=baseline_payload,
        policy=PromotionPolicy(
            min_accuracy_lift=args.min_accuracy_lift,
            min_test_samples=args.min_test_samples,
        ),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(decision.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote promotion decision to {output}")
    return 0 if decision.approved else 1


def _llm_eval(args: argparse.Namespace) -> int:
    payload = run_guardrail_evals()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote LLM guardrail eval to {output}")
    return 0 if payload["failed"] == 0 else 1


def _llm_role_registry(args: argparse.Namespace) -> int:
    try:
        result = run_llm_role_registry(output_dir=args.output_dir)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM role registry to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote LLM role registry markdown to {result.markdown_path}")
    return result.exit_code


def _llm_training_dataset(args: argparse.Namespace) -> int:
    try:
        result = run_llm_training_dataset(
            role=args.role,
            as_of_date=args.as_of_date,
            source_root=args.source_root,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM training dataset to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote LLM training dataset markdown to {result.markdown_path}")
    return result.exit_code


def _llm_supervise_labels(args: argparse.Namespace) -> int:
    try:
        result = run_llm_supervise_labels(
            role=args.role,
            dataset=args.dataset,
            frontier_model=args.frontier_model,
            output_dir=args.output_dir,
            use_openai=args.use_openai,
            confirm_llm_supervision=args.confirm_llm_supervision,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM supervision labels to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote LLM supervision markdown to {result.markdown_path}")
    return result.exit_code


def _llm_eval_suite(args: argparse.Namespace) -> int:
    try:
        result = run_llm_eval_suite(
            role=args.role,
            candidate=args.candidate,
            holdout=args.holdout,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM eval suite to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote LLM eval suite markdown to {result.markdown_path}")
    return result.exit_code


def _llm_candidate_report(args: argparse.Namespace) -> int:
    try:
        result = run_llm_candidate_report(
            role=args.role,
            baseline_eval=args.baseline_eval,
            candidate_eval=args.candidate_eval,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM candidate report to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote LLM candidate markdown to {result.markdown_path}")
    return result.exit_code


def _llm_training_export(args: argparse.Namespace) -> int:
    try:
        result = run_llm_training_export(
            role=args.role,
            supervised_dataset=args.supervised_dataset,
            output_format=args.output_format,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM training export manifest to {result.output_path}")
    return result.exit_code


def _llm_local_cache_verify(args: argparse.Namespace) -> int:
    try:
        result = run_llm_local_cache_verify(
            model_id=args.model_id,
            registry=args.registry,
            cache_root=args.cache_root,
            output=args.output,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote local LLM cache verification to {result.output_path}")
    if result.status != "READY":
        print("local LLM cache missing", file=sys.stderr)
    return result.exit_code


def _llm_local_smoke(args: argparse.Namespace) -> int:
    try:
        result = run_llm_local_smoke(
            model_id=args.model_id,
            registry=args.registry,
            cache_root=args.cache_root,
            schema_name=args.schema_name,
            prompt=args.prompt,
            output=args.output,
            max_new_tokens=args.max_new_tokens,
            fixture_response=args.fixture_response,
            adapter_manifest=args.adapter_manifest,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote local LLM smoke report to {result.output_path}")
    if result.status != "PASSED":
        print(f"local LLM smoke {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _llm_local_sft(args: argparse.Namespace) -> int:
    try:
        metrics = json.loads(args.metrics_json) if args.metrics_json else {}
        if not isinstance(metrics, dict):
            raise ValueError("--metrics-json must decode to a JSON object")
        result = run_llm_local_sft(
            role=args.role,
            base_model_id=args.base_model_id,
            training_jsonl=args.training_jsonl,
            adapter_dir=args.adapter_dir,
            output=args.output,
            registry=args.registry,
            cache_root=args.cache_root,
            metrics=metrics,
            register_existing_adapter=args.register_existing_adapter,
            epochs=args.epochs,
            learning_rate=args.learning_rate,
            batch_size=args.batch_size,
            gradient_accumulation_steps=args.gradient_accumulation_steps,
            max_steps=args.max_steps,
            lora_rank=args.lora_rank,
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            dtype=args.dtype,
            device=args.device,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote local LLM SFT manifest to {result.output_path}")
    if result.status == "BLOCKED":
        print("local LLM SFT blocked", file=sys.stderr)
    return result.exit_code


def _llm_local_eval_suite(args: argparse.Namespace) -> int:
    try:
        result = run_llm_local_eval_suite(
            role=args.role,
            candidate=args.candidate,
            holdout=args.holdout,
            base_model_id=args.base_model_id,
            adapter_manifest=args.adapter_manifest,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote local LLM eval suite to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote local LLM eval suite markdown to {result.markdown_path}")
    return result.exit_code


def _llm_local_adapter_report(args: argparse.Namespace) -> int:
    try:
        result = run_llm_local_adapter_report(
            role=args.role,
            sft_manifest=args.sft_manifest,
            eval_report=args.eval_report,
            smoke_report=args.smoke_report,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote local LLM adapter report to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote local LLM adapter report markdown to {result.markdown_path}")
    return result.exit_code


def _llm_local_alias_decision(args: argparse.Namespace) -> int:
    try:
        result = run_llm_local_alias_decision(
            role=args.role,
            adapter_report=args.adapter_report,
            reviewer=args.reviewer,
            reason=args.reason,
            decision=args.decision,
            ttl_days=args.ttl_days,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote local LLM alias to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote local LLM alias markdown to {result.markdown_path}")
    return result.exit_code


def _llm_model_alias_decision(args: argparse.Namespace) -> int:
    try:
        result = run_llm_model_alias_decision(
            role=args.role,
            candidate_report=args.candidate_report,
            reviewer=args.reviewer,
            reason=args.reason,
            decision=args.decision,
            ttl_days=args.ttl_days,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM model alias to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote LLM model alias markdown to {result.markdown_path}")
    return result.exit_code


def _llm_adaptive_review(args: argparse.Namespace) -> int:
    try:
        result = run_llm_adaptive_review(
            role=args.role,
            feedback_ledger=args.feedback_ledger,
            eval_report=args.eval_report,
            output_dir=args.output_dir,
            min_corrections_for_supervision=args.min_corrections_for_supervision,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM adaptive review to {result.output_path}")
    if result.markdown_path is not None:
        print(f"wrote LLM adaptive markdown to {result.markdown_path}")
    return result.exit_code


def _not_implemented(message: str):
    def handler(_: argparse.Namespace) -> int:
        print(message, file=sys.stderr)
        return 2

    return handler


def _default_feature_names(records: list[dict[str, object]]) -> tuple[str, ...]:
    return default_model_feature_names(records)


def _parse_feature_names(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(name.strip() for name in value.split(",") if name.strip())


def _with_metadata(result, metadata: dict[str, object]):
    from trading_ai.backtest.engine import BacktestResult

    return BacktestResult(
        config=result.config,
        daily_returns=result.daily_returns,
        equity_curve=result.equity_curve,
        positions=result.positions,
        trades=result.trades,
        metrics=result.metrics,
        metadata=metadata,
    )


def _paper_order_result_to_dict(result) -> dict[str, object]:
    return {
        "accepted": result.accepted,
        "status": result.status,
        "reasons": list(result.reasons),
        "dry_run": result.dry_run,
        "broker_response": _broker_response_to_dict(result.broker_response),
    }


def _paper_order_snapshot_to_dict(order: PaperOrderSnapshot) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "time_in_force": order.time_in_force,
        "status": order.status,
        "notional": order.notional,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "filled_avg_price": order.filled_avg_price,
        "submitted_at": order.submitted_at,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "expires_at": order.expires_at,
    }


def _get_requested_order(
    broker: AlpacaPaperBroker,
    *,
    order_id: str | None,
    client_order_id: str | None,
) -> PaperOrderSnapshot:
    if order_id:
        return broker.get_order(order_id=order_id)
    if client_order_id:
        return broker.get_order_by_client_id(client_order_id)
    raise ValueError("order_id or client_order_id is required")


def _reconcile_order(
    expected_order: dict[str, object],
    current_order: PaperOrderSnapshot | None,
    positions: tuple[PaperPosition, ...],
) -> dict[str, object]:
    differences: list[str] = []
    expected_symbol = str(expected_order.get("symbol", "")).upper()
    if current_order is None:
        differences.append("order_missing")
        return {"matched": False, "differences": differences}

    status = current_order.status.lower()
    if current_order.symbol != expected_symbol:
        differences.append("unexpected_symbol")
    if status in {"canceled", "cancelled"}:
        differences.append("cancelled")
    elif status == "expired":
        differences.append("expired")
    elif current_order.filled_quantity <= 0:
        differences.append("not_filled_yet")
    elif not any(position.symbol == current_order.symbol for position in positions):
        differences.append("filled_without_position")

    return {"matched": not differences, "differences": differences}


def _paper_order_intent_to_dict(order: PaperOrder) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": order.symbol.upper(),
        "side": order.side.lower(),
        "client_order_id": order.client_order_id,
        "type": "market",
        "time_in_force": "day",
    }
    if order.quantity is not None:
        payload["quantity"] = order.quantity
    if order.notional is not None:
        payload["notional"] = order.notional
    return payload


def _paper_preflight_to_dict(decision: PaperPreflightDecision) -> dict[str, object]:
    return {
        "allowed": decision.allowed,
        "reasons": list(decision.reasons),
        "checked_at": decision.checked_at,
        "max_feature_age_days": decision.max_feature_age_days,
    }


def _model_signal_to_dict(signal: ModelSignal) -> dict[str, object]:
    return {
        "timestamp": signal.timestamp,
        "symbol": signal.symbol,
        "probability": signal.probability,
        "threshold": signal.threshold,
        "action": signal.action,
    }


def _select_signal_to_submit(signals: tuple[ModelSignal, ...]) -> ModelSignal | None:
    buy_signals = [signal for signal in signals if signal.action == "buy"]
    if not buy_signals:
        return None
    return max(buy_signals, key=lambda signal: (signal.probability, signal.symbol))


def _signal_client_order_id(signal: ModelSignal) -> str:
    compact_timestamp = "".join(character for character in signal.timestamp if character.isalnum())
    return f"signal-{signal.symbol.lower()}-{compact_timestamp[:16]}"


def _broker_response_to_dict(response) -> object:
    if response is None:
        return None
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    return {"repr": repr(response)}


def _write_json_output(payload: dict[str, object], output_path: str) -> None:
    write_json_artifact(payload, output_path)


def _append_paper_order_ledger(
    args: argparse.Namespace,
    *,
    event_type: str,
    payload: dict[str, object] | None,
    exit_code: int,
    source_path: str | None = None,
) -> None:
    append_paper_ledger_event(
        args.ledger_output,
        paper_order_ledger_event(
            event_type=event_type,
            payload=payload,
            exit_code=exit_code,
            output_path=args.output,
            source_path=source_path,
        ),
    )


def _append_paper_operational_error(args: argparse.Namespace, reason: str) -> None:
    event_type = _paper_operation_event_type(args)
    if event_type is None:
        return
    append_paper_ledger_event(
        args.ledger_output,
        paper_order_ledger_event(
            event_type=event_type,
            payload=None,
            exit_code=2,
            output_path=args.output,
            source_path=args.source_report,
            status="ERROR",
            reasons=[reason],
        ),
    )


def _paper_operation_event_type(args: argparse.Namespace) -> str | None:
    if args.reconcile_order:
        return "paper_reconciliation"
    if args.cancel_order:
        return "paper_cancel_order"
    if args.get_order:
        return "paper_order_query"
    if args.list_orders:
        return "paper_order_list"
    return None


def _read_json_report(path: str) -> dict[str, object]:
    return read_json_artifact(path)


def _read_optional_json_report(path: str | None) -> dict[str, object] | None:
    if not path:
        return None
    return _read_json_report(path)


def _read_optional_mlflow_candidate_review_report(path: str | None) -> dict[str, object] | None:
    if not path:
        return None
    try:
        return _read_json_report(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "schema_version": 1,
            "status": "INVALID",
            "failures": [f"cannot read MLflow paper-candidate review report: {exc}"],
        }


def _resolve_as_of_date(value: str) -> date:
    if value == "today":
        return date.today()
    return _parse_cli_date(value)


def _refresh_manifest(
    records: list[dict[str, object]],
    *,
    source: str,
    dataset_path: Path,
    request: dict[str, object],
) -> dict[str, object]:
    manifest = build_dataset_manifest(records, source=source)
    manifest["dataset_path"] = str(dataset_path)
    manifest["request"] = request
    return manifest


def _write_refresh_freshness(
    payload: dict[str, object],
    *,
    freshness_path: Path,
    model_path: str,
    feature_names: tuple[str, ...],
    raw_path: Path,
    features_path: Path,
) -> None:
    payload["model_path"] = str(model_path)
    payload["feature_names"] = list(feature_names)
    payload["raw_path"] = str(raw_path)
    payload["features_path"] = str(features_path)
    freshness_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _parse_cli_date(value: str) -> date:
    return date.fromisoformat(value)


def _paper_account_to_dict(account) -> dict[str, object]:
    return {
        "account_id": account.account_id,
        "status": account.status,
        "cash": account.cash,
        "equity": account.equity,
        "buying_power": account.buying_power,
    }


def _paper_position_to_dict(position) -> dict[str, object]:
    return {
        "symbol": position.symbol,
        "quantity": position.quantity,
        "market_value": position.market_value,
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
