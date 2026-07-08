"""Command-line interface for the trading AI research MVP."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.ai.events import AiEventOperationalError, run_ai_event_extract
from trading_ai.ai.features import AiFeatureOperationalError, run_ai_feature_build
from trading_ai.backtest.engine import BacktestConfig, run_momentum_vol_target_backtest
from trading_ai.cli_paper import PaperCliHandlers, add_paper_subcommands
from trading_ai.config import ConfigError, load_risk_config, load_universe_config
from trading_ai.data.alpaca_market_data import AlpacaMarketDataError, run_market_data_fetch
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
from trading_ai.evaluation.ai_feature_attribution import (
    AiFeatureAttributionOperationalError,
    run_ai_feature_attribution_report,
)
from trading_ai.evaluation.approved_data import ApprovedEvaluationOperationalError, evaluate_approved_data
from trading_ai.evaluation.forecasting_challenger import (
    ForecastingChallengerOperationalError,
    run_forecasting_challenger_report,
)
from trading_ai.evaluation.indicator_activation import (
    DEFAULT_OUTPUT_DIR as INDICATOR_ACTIVATION_DEFAULT_OUTPUT_DIR,
    MIN_RELATIVE_MARGIN as INDICATOR_ACTIVATION_MIN_RELATIVE_MARGIN,
    feature_config_from_activation,
    load_indicator_activation,
    run_indicator_activation_report,
)
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
from trading_ai.evaluation.trading_model_benchmark import run_trading_model_benchmark
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
from trading_ai.execution.cross_asset_session_plan import (
    DEFAULT_FOREX_READINESS as CROSS_ASSET_DEFAULT_FOREX_READINESS,
    DEFAULT_FUTURES_READINESS as CROSS_ASSET_DEFAULT_FUTURES_READINESS,
    DEFAULT_MARKDOWN_OUTPUT as CROSS_ASSET_SESSION_DEFAULT_MARKDOWN_OUTPUT,
    DEFAULT_OUTPUT as CROSS_ASSET_SESSION_DEFAULT_OUTPUT,
    CrossAssetSessionPlanOperationalError,
    run_cross_asset_session_plan,
)
from trading_ai.execution.futures_readiness import (
    DEFAULT_CONFIG as FUTURES_READINESS_DEFAULT_CONFIG,
    DEFAULT_MARKDOWN_OUTPUT as FUTURES_READINESS_DEFAULT_MARKDOWN_OUTPUT,
    DEFAULT_OUTPUT as FUTURES_READINESS_DEFAULT_OUTPUT,
    FuturesReadinessOperationalError,
    run_futures_readiness_report,
)
from trading_ai.execution.futures_research import FuturesResearchOperationalError, run_futures_research_scaffold
from trading_ai.execution.autonomy_incident_sync import (
    DEFAULT_OUTPUT_DIR as AUTONOMY_INCIDENT_SYNC_DEFAULT_OUTPUT_DIR,
    run_autonomy_incident_sync,
)
from trading_ai.execution.autonomy_level import (
    AUTONOMY_LEVELS,
    AUTONOMY_MARKETS,
    DEFAULT_STATE_DIR as AUTONOMY_DEFAULT_STATE_DIR,
    certify_autonomy_promotion,
    load_autonomy_state,
    record_autonomy_incident,
    resolve_autonomy_incident,
)
from trading_ai.execution.forex_readiness import (
    DEFAULT_CONFIG as FOREX_READINESS_DEFAULT_CONFIG,
    DEFAULT_MARKDOWN_OUTPUT as FOREX_READINESS_DEFAULT_MARKDOWN_OUTPUT,
    DEFAULT_OUTPUT as FOREX_READINESS_DEFAULT_OUTPUT,
    ForexReadinessOperationalError,
    run_forex_readiness_report,
)
from trading_ai.execution.live_canary import run_live_canary
from trading_ai.execution.live_alpaca import AlpacaLiveBroker
from trading_ai.execution.live_connection import build_alpaca_live_runtime
from trading_ai.execution.live_execute_session import run_live_execute_session
from trading_ai.execution.live_readiness import run_live_readiness_report
from trading_ai.execution.live_reconciliation import LivePosition
from trading_ai.execution.live_rehearsal import run_live_rehearsal
from trading_ai.execution.live_safe_flatten import run_live_safe_flatten
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
from trading_ai.execution.paper_common import (
    as_of_date_to_iso,
    read_json_artifact,
    write_json_artifact,
    write_text_artifact,
)
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
from trading_ai.execution.paper_eod_position_plan import (
    PaperEodPositionPlanOperationalError,
    run_paper_eod_position_plan,
)
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
from trading_ai.execution.paper_n0_certification import (
    DEFAULT_MAX_DRAWDOWN_PCT as N0_CERTIFICATION_DEFAULT_MAX_DRAWDOWN_PCT,
    DEFAULT_MIN_CLEAN_DAYS as N0_CERTIFICATION_DEFAULT_MIN_CLEAN_DAYS,
    DEFAULT_OUTPUT_DIR as N0_CERTIFICATION_DEFAULT_OUTPUT_DIR,
    run_paper_n0_certification,
)
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
from trading_ai.execution.paper_risk_state import DEFAULT_RISK_STATE_PATH
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
from trading_ai.execution.paper_signal_approval import (
    DEFAULT_REGISTRY_DIR as SIGNAL_APPROVAL_DEFAULT_REGISTRY_DIR,
    compute_plan_hash,
    evaluate_signal_approval_gate,
    load_signal_approval_registry,
)
from trading_ai.execution.paper_statement import PaperStatementOperationalError, run_paper_statement_validate
from trading_ai.execution.paper_strategy_quality import PaperStrategyQualityOperationalError, run_paper_strategy_quality
from trading_ai.execution.paper_swing_declarations import record_swing_declaration
from trading_ai.execution.paper_telegram_status import (
    PaperTelegramStatusOperationalError,
    run_paper_telegram_status,
)
from trading_ai.execution.paper_telegram_history import (
    PaperTelegramHistoryOperationalError,
    run_paper_telegram_history,
)
from trading_ai.execution.paper_telegram_send import (
    PaperTelegramSendOperationalError,
    run_paper_telegram_send,
)
from trading_ai.execution.paper_telegram_notify import (
    PaperTelegramNotifyOperationalError,
    run_paper_telegram_notify,
)
from trading_ai.execution.paper_trial_day import run_paper_trial_day
from trading_ai.execution.paper_weekly_summary import PaperWeeklySummaryOperationalError, run_paper_weekly_summary
from trading_ai.execution.telegram_control import (
    DEFAULT_APPLY_OUTPUT as TELEGRAM_CONTROL_APPLY_DEFAULT_OUTPUT,
    DEFAULT_DISPATCH_OUTPUT as TELEGRAM_CONTROL_DISPATCH_DEFAULT_OUTPUT,
    DEFAULT_PLAN_OUTPUT as TELEGRAM_CONTROL_PLAN_DEFAULT_OUTPUT,
    TelegramControlOperationalError,
    run_telegram_control_apply,
    run_telegram_control_dispatch,
    run_telegram_control_inbox,
    run_telegram_control_plan,
)
from trading_ai.features.engineering import (
    build_features,
    default_model_feature_names,
    has_finite_feature_value,
)
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
    run_llm_local_runtime,
    run_llm_local_sft,
    run_llm_local_smoke,
)
from trading_ai.llm.provider_benchmark import run_llm_provider_benchmark
from trading_ai.models.baseline import (
    LogisticBaselineConfig,
    build_supervised_examples,
    build_triple_barrier_examples,
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

    fetch_market_data = subparsers.add_parser("fetch-market-data")
    fetch_market_data.add_argument("--config", default="configs/universe.yml")
    fetch_market_data.add_argument("--from", dest="start", required=True)
    fetch_market_data.add_argument("--to", dest="end", required=True)
    fetch_market_data.add_argument("--output", default="data/incoming/fresh_source.csv")
    fetch_market_data.set_defaults(func=_fetch_market_data)

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

    trading_model_benchmark = subparsers.add_parser("trading-model-benchmark")
    trading_model_benchmark.add_argument("--approved-dir", required=True)
    trading_model_benchmark.add_argument("--from", dest="start", required=True)
    trading_model_benchmark.add_argument("--to", dest="end", required=True)
    trading_model_benchmark.add_argument("--as-of-date", required=True)
    trading_model_benchmark.add_argument("--config", default="configs/universe.yml")
    trading_model_benchmark.add_argument("--risk", default="configs/risk.yml")
    trading_model_benchmark.add_argument("--signal-model", default="models/latest_model.json")
    trading_model_benchmark.add_argument("--output-dir", default="reports/tmp/trading_model_benchmark")
    trading_model_benchmark.add_argument("--embargo", type=int, default=1)
    trading_model_benchmark.add_argument("--ai-features")
    trading_model_benchmark.add_argument("--forecast-features")
    trading_model_benchmark.set_defaults(func=_trading_model_benchmark)

    ai_event_extract = subparsers.add_parser("ai-event-extract")
    ai_event_extract.add_argument("--as-of-date", required=True)
    ai_event_extract.add_argument("--input-jsonl", required=True)
    ai_event_extract.add_argument("--config", default="configs/universe.yml")
    ai_event_extract.add_argument("--provider-config", default="configs/ai_feature_sources.yml")
    ai_event_extract.add_argument("--provider", default="manual_jsonl", choices=("manual_jsonl", "external_api"))
    ai_event_extract.add_argument("--output-dir", default="reports/tmp/ai_events")
    ai_event_extract.set_defaults(func=_ai_event_extract)

    ai_feature_build = subparsers.add_parser("ai-feature-build")
    ai_feature_build.add_argument("--as-of-date", required=True)
    ai_feature_build.add_argument("--features", required=True)
    ai_feature_build.add_argument("--events", required=True)
    ai_feature_build.add_argument("--config", default="configs/universe.yml")
    ai_feature_build.add_argument("--provider-config", default="configs/ai_feature_sources.yml")
    ai_feature_build.add_argument("--provider", default="manual_jsonl", choices=("manual_jsonl", "external_api"))
    ai_feature_build.add_argument("--output-dir", default="reports/tmp/ai_features")
    ai_feature_build.set_defaults(func=_ai_feature_build)

    forecasting_challenger = subparsers.add_parser("forecasting-challenger-report")
    forecasting_challenger.add_argument("--as-of-date", required=True)
    forecasting_challenger.add_argument("--features", required=True)
    forecasting_challenger.add_argument("--config", default="configs/universe.yml")
    forecasting_challenger.add_argument("--output-dir", default="reports/tmp/forecasting_challenger")
    forecasting_challenger.add_argument("--lookback-days", type=int, default=5)
    forecasting_challenger.set_defaults(func=_forecasting_challenger_report)

    ai_feature_attribution = subparsers.add_parser("ai-feature-attribution-report")
    ai_feature_attribution.add_argument("--as-of-date", required=True)
    ai_feature_attribution.add_argument("--baseline-ranking", required=True)
    ai_feature_attribution.add_argument("--ai-ranking", required=True)
    ai_feature_attribution.add_argument("--output-dir", default="reports/tmp/ai_feature_attribution")
    ai_feature_attribution.add_argument("--min-sharpe-delta", type=float, default=0.05)
    ai_feature_attribution.add_argument("--max-drawdown-worsening", type=float, default=0.02)
    ai_feature_attribution.add_argument("--max-cost-delta", type=float, default=0.01)
    ai_feature_attribution.set_defaults(func=_ai_feature_attribution_report)

    indicator_activation = subparsers.add_parser("indicator-activation")
    indicator_activation.add_argument("--as-of-date", required=True)
    indicator_activation.add_argument("--dataset", required=True)
    indicator_activation.add_argument("--output-dir", default=INDICATOR_ACTIVATION_DEFAULT_OUTPUT_DIR)
    indicator_activation.add_argument(
        "--min-relative-margin", type=float, default=INDICATOR_ACTIVATION_MIN_RELATIVE_MARGIN
    )
    indicator_activation.set_defaults(func=_indicator_activation)

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
    features.add_argument(
        "--indicator-activation-dir",
        default=None,
        help=(
            "Opt-in: read the evidence-gated indicator-activation recommendation from this "
            "directory and use its FeatureConfig (baseline by default, extended only when "
            "recommended and un-tampered). Omit to keep today's baseline-only behavior."
        ),
    )
    features.add_argument(
        "--as-of-date",
        default="today",
        help="Only consulted when --indicator-activation-dir is set.",
    )
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
    train.add_argument(
        "--feature-names",
        default=None,
        help=(
            "Opt-in: comma-separated feature names to train on (e.g. 'rsi_14,macd_hist,bb_pct_b'). "
            "Each name must have at least one finite value in the dataset. "
            "Omit to keep today's default-model feature selection."
        ),
    )
    train.add_argument(
        "--standardize-features",
        action="store_true",
        help=(
            "Opt-in: compute per-feature mean/std on the TRAIN split only and "
            "apply the transformation to inputs at inference. Stats are saved "
            "in the model artifact. Omit to keep today's identity (raw) behavior."
        ),
    )
    train.add_argument(
        "--labeling",
        choices=("direction", "triple_barrier"),
        default="direction",
        help=(
            "Labeling scheme. 'direction' (default) preserves today's "
            "byte-identical next_close > close behavior. 'triple_barrier' "
            "labels each row by a vol-scaled upper/lower barrier over "
            "--label-horizon bars (López de Prado), with a sign-based time-out."
        ),
    )
    train.add_argument(
        "--label-horizon",
        type=int,
        default=5,
        help=(
            "Triple-barrier only: forward window in bars used to detect the "
            "first barrier touch. Also used as the embargo between train and "
            "test (and per walk-forward window) to prevent label leakage."
        ),
    )
    train.add_argument(
        "--label-atr-mult",
        type=float,
        default=1.0,
        help=(
            "Triple-barrier only: barrier half-width expressed as a multiple "
            "of the per-row volatility unit (vol_column). Must be > 0."
        ),
    )
    train.add_argument(
        "--vol-column",
        default="atr_14",
        help=(
            "Triple-barrier only: column in the dataset that supplies the "
            "per-row volatility unit used to scale upper/lower barriers."
        ),
    )
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

    llm_provider = subparsers.add_parser("llm-provider-benchmark")
    llm_provider.add_argument("--provider", required=True, choices=("nvidia-nim",))
    llm_provider.add_argument("--model-suite", required=True)
    llm_provider.add_argument("--role", required=True)
    llm_provider.add_argument("--as-of-date", required=True)
    llm_provider.add_argument("--output-dir", default="reports/tmp/llm_provider_benchmark")
    llm_provider.add_argument("--confirm-external-llm", action="store_true")
    llm_provider.set_defaults(func=_llm_provider_benchmark)

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

    llm_local_runtime = subparsers.add_parser("llm-local-runtime")
    llm_local_runtime.add_argument("--device-root", default="/dev")
    llm_local_runtime.add_argument("--output", default="reports/tmp/llm_local/runtime.json")
    llm_local_runtime.set_defaults(func=_llm_local_runtime)

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

    telegram_control = subparsers.add_parser("telegram-control-inbox")
    telegram_control.add_argument("--as-of-date", required=True)
    telegram_control.add_argument("--updates", required=True)
    telegram_control.add_argument("--allowed-chat-id", action="append", required=True)
    telegram_control.add_argument("--allowed-user-id", action="append", required=True)
    telegram_control.add_argument("--environment", default="paper", choices=("paper", "live"))
    telegram_control.add_argument("--state")
    telegram_control.add_argument("--output", default="reports/tmp/telegram_control/latest.json")
    telegram_control.add_argument("--ledger-output")
    telegram_control.set_defaults(func=_telegram_control_inbox)

    telegram_apply = subparsers.add_parser("telegram-control-apply")
    telegram_apply.add_argument("--as-of-date", required=True)
    telegram_apply.add_argument("--inbox", required=True)
    telegram_apply.add_argument("--risk-state-path", default=DEFAULT_RISK_STATE_PATH)
    telegram_apply.add_argument("--status-report")
    telegram_apply.add_argument("--history-report")
    telegram_apply.add_argument("--signal-plan")
    telegram_apply.add_argument("--signal-approval-registry-dir", default=SIGNAL_APPROVAL_DEFAULT_REGISTRY_DIR)
    telegram_apply.add_argument("--state")
    telegram_apply.add_argument("--output", default=TELEGRAM_CONTROL_APPLY_DEFAULT_OUTPUT)
    telegram_apply.add_argument("--ledger-output")
    telegram_apply.add_argument("--confirm-telegram-control", action="store_true")
    telegram_apply.set_defaults(func=_telegram_control_apply)

    telegram_plan = subparsers.add_parser("telegram-control-plan")
    telegram_plan.add_argument("--as-of-date", required=True)
    telegram_plan.add_argument("--apply", required=True)
    telegram_plan.add_argument("--output", default=TELEGRAM_CONTROL_PLAN_DEFAULT_OUTPUT)
    telegram_plan.add_argument("--ledger-output")
    telegram_plan.set_defaults(func=_telegram_control_plan)

    telegram_dispatch = subparsers.add_parser("telegram-control-dispatch")
    telegram_dispatch.add_argument("--as-of-date", required=True)
    telegram_dispatch.add_argument("--plan", required=True)
    telegram_dispatch.add_argument("--output", default=TELEGRAM_CONTROL_DISPATCH_DEFAULT_OUTPUT)
    telegram_dispatch.add_argument("--ledger-output")
    telegram_dispatch.add_argument("--dry-run", action="store_true", default=True)
    telegram_dispatch.set_defaults(func=_telegram_control_dispatch)

    live_readiness = subparsers.add_parser("live-readiness-report")
    live_readiness.add_argument("--as-of-date", required=True)
    live_readiness.add_argument("--phase-review", required=True)
    live_readiness.add_argument("--campaign-report", required=True)
    live_readiness.add_argument("--performance-report", required=True)
    live_readiness.add_argument("--permissions", required=True)
    live_readiness.add_argument("--reviewer", required=True)
    live_readiness.add_argument("--reason", required=True)
    live_readiness.add_argument("--ai-value-report")
    live_readiness.add_argument("--require-ai-evidence", action="store_true")
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

    live_flatten = subparsers.add_parser("live-safe-flatten")
    live_flatten.add_argument("--as-of-date", required=True)
    live_flatten.add_argument("--positions-fixture", required=True)
    live_flatten.add_argument("--allowlist", nargs="+", required=True)
    live_flatten.add_argument("--reviewer", required=True)
    live_flatten.add_argument("--reason", required=True)
    live_flatten.add_argument("--output-dir", default="reports/tmp/live_safe_flatten")
    live_flatten.add_argument("--dry-run", action="store_true", default=True)
    live_flatten.set_defaults(func=_live_safe_flatten)

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
    live_canary.add_argument("--risk-live")
    live_canary.add_argument("--reference-price", type=float)
    live_canary.add_argument("--confirm-real-submit")
    live_canary.add_argument("--universe", default="configs/universe.yml")
    live_canary.add_argument("--autonomy-state-dir", default=AUTONOMY_DEFAULT_STATE_DIR)
    live_canary.add_argument("--autonomy-market", choices=AUTONOMY_MARKETS, default="equities")
    live_canary.add_argument("--signal-plan", default=None)
    live_canary.add_argument("--approval-registry-dir", default=SIGNAL_APPROVAL_DEFAULT_REGISTRY_DIR)
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
            paper_eod_position_plan=_paper_eod_position_plan,
            paper_swing_declare=_paper_swing_declare,
            paper_safe_flatten=_paper_safe_flatten,
            paper_close_session=_paper_close_session,
            paper_observability=_paper_observability,
            paper_monitor=_paper_monitor,
            paper_telegram_status=_paper_telegram_status,
            paper_telegram_history=_paper_telegram_history,
            paper_telegram_send=_paper_telegram_send,
            paper_telegram_notify=_paper_telegram_notify,
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

    cross_asset_session = subparsers.add_parser("cross-asset-session-plan")
    cross_asset_session.add_argument("--as-of-date", required=True)
    cross_asset_session.add_argument("--positions", required=True)
    cross_asset_session.add_argument("--current-time", required=True)
    cross_asset_session.add_argument("--futures-readiness", default=CROSS_ASSET_DEFAULT_FUTURES_READINESS)
    cross_asset_session.add_argument("--forex-readiness", default=CROSS_ASSET_DEFAULT_FOREX_READINESS)
    cross_asset_session.add_argument("--futures-session-close-time", default="17:00")
    cross_asset_session.add_argument("--forex-weekend-close-time", default="21:00")
    cross_asset_session.add_argument("--flatten-window-minutes", type=int, default=30)
    cross_asset_session.add_argument("--longer-term-symbol", action="append", default=[])
    cross_asset_session.add_argument("--output", default=CROSS_ASSET_SESSION_DEFAULT_OUTPUT)
    cross_asset_session.add_argument("--markdown-output", default=CROSS_ASSET_SESSION_DEFAULT_MARKDOWN_OUTPUT)
    cross_asset_session.add_argument("--ledger-output")
    cross_asset_session.set_defaults(func=_cross_asset_session_plan)

    forex_readiness = subparsers.add_parser("forex-readiness-report")
    forex_readiness.add_argument("--config", default=FOREX_READINESS_DEFAULT_CONFIG)
    forex_readiness.add_argument("--output", default=FOREX_READINESS_DEFAULT_OUTPUT)
    forex_readiness.add_argument("--markdown-output", default=FOREX_READINESS_DEFAULT_MARKDOWN_OUTPUT)
    forex_readiness.set_defaults(func=_forex_readiness_report)

    autonomy_status = subparsers.add_parser("autonomy-status")
    autonomy_status.add_argument("--market", required=True, choices=AUTONOMY_MARKETS)
    autonomy_status.add_argument("--state-dir", default=AUTONOMY_DEFAULT_STATE_DIR)
    autonomy_status.add_argument("--output")
    autonomy_status.set_defaults(func=_autonomy_status)

    autonomy_certify = subparsers.add_parser("autonomy-certify")
    autonomy_certify.add_argument("--market", required=True, choices=AUTONOMY_MARKETS)
    autonomy_certify.add_argument("--target-level", required=True, choices=AUTONOMY_LEVELS)
    autonomy_certify.add_argument("--reviewer", required=True)
    autonomy_certify.add_argument("--reason", required=True)
    autonomy_certify.add_argument("--clean-days", type=int, required=True)
    autonomy_certify.add_argument("--evidence-kind", required=True)
    autonomy_certify.add_argument("--artifact-hash", required=True)
    autonomy_certify.add_argument("--state-dir", default=AUTONOMY_DEFAULT_STATE_DIR)
    autonomy_certify.add_argument("--output")
    autonomy_certify.set_defaults(func=_autonomy_certify)

    autonomy_incident = subparsers.add_parser("autonomy-incident")
    autonomy_incident.add_argument("--market", required=True, choices=AUTONOMY_MARKETS)
    autonomy_incident.add_argument("--severity", required=True, choices=("grave", "warning", "info"))
    autonomy_incident.add_argument("--source", required=True)
    autonomy_incident.add_argument("--reason", required=True)
    autonomy_incident.add_argument("--state-dir", default=AUTONOMY_DEFAULT_STATE_DIR)
    autonomy_incident.add_argument("--output")
    autonomy_incident.set_defaults(func=_autonomy_incident)

    autonomy_resolve_incident = subparsers.add_parser("autonomy-resolve-incident")
    autonomy_resolve_incident.add_argument("--market", required=True, choices=AUTONOMY_MARKETS)
    autonomy_resolve_incident.add_argument("--reviewer", required=True)
    autonomy_resolve_incident.add_argument("--reason", required=True)
    autonomy_resolve_incident.add_argument("--state-dir", default=AUTONOMY_DEFAULT_STATE_DIR)
    autonomy_resolve_incident.set_defaults(func=_autonomy_resolve_incident)

    autonomy_incident_sync = subparsers.add_parser("autonomy-incident-sync")
    autonomy_incident_sync.add_argument("--as-of-date", required=True)
    autonomy_incident_sync.add_argument("--market", required=True, choices=AUTONOMY_MARKETS)
    autonomy_incident_sync.add_argument("--breaker-state")
    autonomy_incident_sync.add_argument("--reconciliation-report")
    autonomy_incident_sync.add_argument("--risk-state")
    autonomy_incident_sync.add_argument("--autonomy-state-dir", default=AUTONOMY_DEFAULT_STATE_DIR)
    autonomy_incident_sync.add_argument("--output-dir", default=AUTONOMY_INCIDENT_SYNC_DEFAULT_OUTPUT_DIR)
    autonomy_incident_sync.set_defaults(func=_autonomy_incident_sync)

    signal_approval_status = subparsers.add_parser("paper-signal-approval-status")
    signal_approval_status.add_argument("--as-of-date", required=True)
    signal_approval_status.add_argument("--registry-dir", default=SIGNAL_APPROVAL_DEFAULT_REGISTRY_DIR)
    signal_approval_status.add_argument("--plan")
    signal_approval_status.add_argument("--output")
    signal_approval_status.set_defaults(func=_paper_signal_approval_status)

    paper_n0_certification = subparsers.add_parser("paper-n0-certification")
    paper_n0_certification.add_argument("--as-of-date", required=True)
    paper_n0_certification.add_argument("--market", choices=AUTONOMY_MARKETS, default="equities")
    paper_n0_certification.add_argument("--session-ledger", action="append", required=True)
    paper_n0_certification.add_argument("--performance-report", required=True)
    paper_n0_certification.add_argument(
        "--min-clean-days", type=int, default=N0_CERTIFICATION_DEFAULT_MIN_CLEAN_DAYS
    )
    paper_n0_certification.add_argument(
        "--max-drawdown-pct", type=float, default=N0_CERTIFICATION_DEFAULT_MAX_DRAWDOWN_PCT
    )
    paper_n0_certification.add_argument("--output-dir", default=N0_CERTIFICATION_DEFAULT_OUTPUT_DIR)
    paper_n0_certification.set_defaults(func=_paper_n0_certification)
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


def _fetch_market_data(args: argparse.Namespace) -> int:
    try:
        result = run_market_data_fetch(
            config=args.config,
            start=args.start,
            end=args.end,
            output=args.output,
        )
    except (ConfigError, AlpacaMarketDataError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"fetch-market-data status={result.status} rows={result.payload.get('row_count')} output={args.output}")
    if result.status == "BLOCKED":
        print("fetch-market-data blocked", file=sys.stderr)
    return result.exit_code


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


def _trading_model_benchmark(args: argparse.Namespace) -> int:
    try:
        result = run_trading_model_benchmark(
            approved_dir=args.approved_dir,
            start=args.start,
            end=args.end,
            as_of_date=args.as_of_date,
            config=args.config,
            risk=args.risk,
            signal_model=args.signal_model,
            output_dir=args.output_dir,
            embargo=args.embargo,
            ai_features=args.ai_features,
            forecast_features=args.forecast_features,
        )
    except (ConfigError, ModelResearchOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote trading model benchmark ranking to {result.ranking_path}")
    print(f"wrote trading model benchmark markdown to {result.markdown_path}")
    print(f"wrote trading model candidate spec to {result.candidate_spec_path}")
    if result.exit_code != 0:
        print(f"trading-model-benchmark {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _forecasting_challenger_report(args: argparse.Namespace) -> int:
    try:
        result = run_forecasting_challenger_report(
            as_of_date=args.as_of_date,
            features=args.features,
            config=args.config,
            output_dir=args.output_dir,
            lookback_days=args.lookback_days,
        )
    except (ForecastingChallengerOperationalError, ConfigError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote forecasting challenger features to {result.features_path}")
    print(f"wrote forecasting challenger report to {result.report_path}")
    if result.exit_code != 0:
        print(f"forecasting-challenger-report {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _ai_feature_attribution_report(args: argparse.Namespace) -> int:
    try:
        result = run_ai_feature_attribution_report(
            as_of_date=args.as_of_date,
            baseline_ranking=args.baseline_ranking,
            ai_ranking=args.ai_ranking,
            output_dir=args.output_dir,
            min_sharpe_delta=args.min_sharpe_delta,
            max_drawdown_worsening=args.max_drawdown_worsening,
            max_cost_delta=args.max_cost_delta,
        )
    except (AiFeatureAttributionOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote AI feature attribution report to {result.report_path}")
    print(f"wrote AI feature attribution markdown to {result.markdown_path}")
    if result.exit_code != 0:
        print(f"ai-feature-attribution-report {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _indicator_activation(args: argparse.Namespace) -> int:
    result = run_indicator_activation_report(
        as_of_date=args.as_of_date,
        dataset=args.dataset,
        output_dir=args.output_dir,
        min_relative_margin=args.min_relative_margin,
    )
    print(f"wrote indicator activation report to {result.output_path}")
    print(f"indicator-activation recommendation: {result.payload.get('recommendation')}")
    if result.exit_code != 0:
        print(f"indicator-activation {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _ai_event_extract(args: argparse.Namespace) -> int:
    try:
        result = run_ai_event_extract(
            as_of_date=args.as_of_date,
            input_jsonl=args.input_jsonl,
            config=args.config,
            provider_config=args.provider_config,
            provider=args.provider,
            output_dir=args.output_dir,
        )
    except (AiEventOperationalError, ConfigError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote AI events to {result.events_path}")
    print(f"wrote AI event manifest to {result.manifest_path}")
    if result.exit_code != 0:
        print(f"ai-event-extract {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _ai_feature_build(args: argparse.Namespace) -> int:
    try:
        result = run_ai_feature_build(
            as_of_date=args.as_of_date,
            features=args.features,
            events=args.events,
            config=args.config,
            provider_config=args.provider_config,
            provider=args.provider,
            output_dir=args.output_dir,
        )
    except (AiFeatureOperationalError, ConfigError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote AI features to {result.features_path}")
    print(f"wrote AI feature manifest to {result.manifest_path}")
    if result.exit_code != 0:
        print(f"ai-feature-build {result.status.lower()}", file=sys.stderr)
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

    activation_dir = getattr(args, "indicator_activation_dir", None)
    if not activation_dir:
        # Default path: byte-for-byte identical to pre-activation behavior.
        features = build_features(records)
        write_records(features, args.output)
        print(f"wrote {len(features)} feature rows to {args.output}")
        return 0

    as_of_date = as_of_date_to_iso(getattr(args, "as_of_date", None) or "today")
    activation = load_indicator_activation(as_of_date=as_of_date, output_dir=activation_dir)
    feature_config = feature_config_from_activation(activation)
    features = build_features(records, feature_config)
    write_records(features, args.output)
    activation_manifest_path = Path(args.output).with_name(Path(args.output).stem + ".indicator_activation.json")
    write_json_artifact(
        {
            "as_of_date": as_of_date,
            "indicator_activation_dir": str(activation_dir),
            "recommendation_used": activation.get("recommendation"),
            "fail_closed": bool(activation.get("fail_closed", False)),
            "reason": activation.get("reason"),
            "feature_config_used": {
                "rsi_window": feature_config.rsi_window,
                "macd_fast": feature_config.macd_fast,
                "macd_slow": feature_config.macd_slow,
                "macd_signal": feature_config.macd_signal,
                "bb_window": feature_config.bb_window,
                "bb_n_std": feature_config.bb_n_std,
            },
        },
        activation_manifest_path,
    )
    print(f"wrote {len(features)} feature rows to {args.output}")
    print(f"wrote indicator activation manifest to {activation_manifest_path}")
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
    broker_date = _parse_cli_date(args.as_of_date) if args.as_of_date else date.today()
    broker = AlpacaPaperBroker(
        client=client,
        allowlist=universe.symbols,
        risk_limits=risk,
        dry_run=dry_run,
        today=lambda: broker_date,
    )
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
            as_of_date=broker_date,
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
            risk_state_path=args.risk_state_path,
            output=args.output,
            markdown_output=args.markdown_output,
        )
    except PaperPositionWatchOperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper position watch to {result.output_path}")
    print(f"wrote paper position watch markdown to {result.markdown_path}")
    return result.exit_code


def _paper_eod_position_plan(args: argparse.Namespace) -> int:
    try:
        result = run_paper_eod_position_plan(
            as_of_date=args.as_of_date,
            position_watch=args.position_watch,
            current_time=args.current_time,
            market_close_time=args.market_close_time,
            flatten_window_minutes=args.flatten_window_minutes,
            longer_term_symbols=args.longer_term_symbol,
            swing_registry_dir=args.swing_registry_dir,
            swing_lookback_days=args.swing_lookback_days,
            timezone=args.timezone,
            output=args.output,
            markdown_output=args.markdown_output,
            ledger_output=args.ledger_output,
        )
    except (PaperEodPositionPlanOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper EOD position plan to {result.output_path}")
    print(f"wrote paper EOD position plan markdown to {result.markdown_path}")
    if result.exit_code != 0:
        print(f"paper EOD position plan {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_swing_declare(args: argparse.Namespace) -> int:
    try:
        decision = record_swing_declaration(
            as_of_date=args.as_of_date,
            symbol=args.symbol,
            plan_hash=args.plan_hash,
            thesis=args.thesis,
            max_overnight_loss_pct=args.max_overnight_loss_pct,
            expires_on=args.expires_on,
            registry_dir=args.registry_dir,
        )
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote swing declaration decision to {decision.output_path}")
    if decision.status != "OK":
        print(f"swing declaration {decision.status.lower()}", file=sys.stderr)
        for blocker in decision.payload.get("blockers") or []:
            print(blocker, file=sys.stderr)
    return decision.exit_code


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


def _paper_telegram_status(args: argparse.Namespace) -> int:
    try:
        result = run_paper_telegram_status(
            as_of_date=args.as_of_date,
            performance=args.performance,
            position_watch=args.position_watch,
            forecast_report=args.forecast_report,
            signal_plan=args.signal_plan,
            eod_position_plan=args.eod_position_plan,
            operator_status=args.operator_status,
            output=args.output,
            autonomy_state_dir=args.autonomy_state_dir,
            autonomy_market=args.autonomy_market,
            n0_certification=args.n0_certification,
        )
    except (PaperTelegramStatusOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper Telegram status to {result.output_path}")
    if result.exit_code != 0:
        print(f"paper Telegram status {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_telegram_history(args: argparse.Namespace) -> int:
    try:
        result = run_paper_telegram_history(
            as_of_date=args.as_of_date,
            performance=args.performance,
            weekly_summary=args.weekly_summary,
            ledger_inputs=args.ledger_input,
            max_events=args.max_events,
            output=args.output,
        )
    except (PaperTelegramHistoryOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper Telegram history to {result.output_path}")
    if result.exit_code != 0:
        print(f"paper Telegram history {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_telegram_send(args: argparse.Namespace) -> int:
    try:
        result = run_paper_telegram_send(
            as_of_date=args.as_of_date,
            artifact=args.artifact,
            output=args.output,
            send_telegram=args.send_telegram,
            telegram_dry_run=args.telegram_dry_run,
        )
    except (PaperTelegramSendOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper Telegram send report to {result.output_path}")
    if result.exit_code != 0:
        print(f"paper Telegram send {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_telegram_notify(args: argparse.Namespace) -> int:
    try:
        result = run_paper_telegram_notify(
            as_of_date=args.as_of_date,
            artifacts=args.artifact,
            output=args.output,
            send_output_dir=args.send_output_dir,
            ledger_output=args.ledger_output,
            send_telegram=args.send_telegram,
            telegram_dry_run=args.telegram_dry_run,
        )
    except (PaperTelegramNotifyOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote paper Telegram notify report to {result.output_path}")
    if result.exit_code != 0:
        print(f"paper Telegram notify {result.status.lower()}", file=sys.stderr)
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
            position_watch=args.position_watch,
            eod_position_plan=args.eod_position_plan,
            telegram_status=args.telegram_status,
            telegram_history=args.telegram_history,
            telegram_dispatch=args.telegram_dispatch,
            ai_value_report=args.ai_value_report,
            cross_asset_session_plan=args.cross_asset_session_plan,
            require_ai_value_ready=args.require_ai_value_ready,
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
            ai_features=args.ai_features,
            forecast_features=args.forecast_features,
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


def _llm_provider_benchmark(args: argparse.Namespace) -> int:
    try:
        result = run_llm_provider_benchmark(
            provider=args.provider,
            model_suite=args.model_suite,
            role=args.role,
            as_of_date=args.as_of_date,
            output_dir=args.output_dir,
            confirm_external_llm=args.confirm_external_llm,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote LLM provider benchmark to {result.output_path}")
    print(f"wrote LLM provider benchmark markdown to {result.markdown_path}")
    if result.status in {"BLOCKED", "ERROR"}:
        print(f"LLM provider benchmark {result.status.lower()}", file=sys.stderr)
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
            features=args.features,
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
            ai_features=args.ai_features,
            forecast_features=args.forecast_features,
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
            position_watch=args.position_watch,
            eod_position_plan=args.eod_position_plan,
            cross_asset_session_plan=args.cross_asset_session_plan,
            telegram_dispatch=args.telegram_dispatch,
            ai_features=args.ai_features,
            forecast_features=args.forecast_features,
            ai_value_report=args.ai_value_report,
            require_ai_value_ready=args.require_ai_value_ready,
            risk_state_path=args.risk_state_path,
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


def _telegram_control_inbox(args: argparse.Namespace) -> int:
    try:
        result = run_telegram_control_inbox(
            as_of_date=args.as_of_date,
            updates=args.updates,
            allowed_chat_ids=args.allowed_chat_id,
            allowed_user_ids=args.allowed_user_id,
            environment=args.environment,
            state=args.state,
            output=args.output,
            ledger_output=args.ledger_output,
        )
    except (TelegramControlOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote telegram control inbox to {result.output_path}")
    if result.exit_code != 0:
        print(f"telegram control inbox {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _telegram_control_apply(args: argparse.Namespace) -> int:
    try:
        result = run_telegram_control_apply(
            as_of_date=args.as_of_date,
            inbox=args.inbox,
            risk_state_path=args.risk_state_path,
            status_report=args.status_report,
            history_report=args.history_report,
            signal_plan=args.signal_plan,
            signal_approval_registry_dir=args.signal_approval_registry_dir,
            state=args.state,
            output=args.output,
            ledger_output=args.ledger_output,
            confirm_telegram_control=args.confirm_telegram_control,
        )
    except (TelegramControlOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote telegram control apply report to {result.output_path}")
    if result.exit_code != 0:
        print(f"telegram control apply {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _telegram_control_plan(args: argparse.Namespace) -> int:
    try:
        result = run_telegram_control_plan(
            as_of_date=args.as_of_date,
            apply_report=args.apply,
            output=args.output,
            ledger_output=args.ledger_output,
        )
    except (TelegramControlOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote telegram control plan to {result.output_path}")
    if result.exit_code != 0:
        print(f"telegram control plan {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _telegram_control_dispatch(args: argparse.Namespace) -> int:
    try:
        result = run_telegram_control_dispatch(
            as_of_date=args.as_of_date,
            plan=args.plan,
            output=args.output,
            ledger_output=args.ledger_output,
            dry_run=args.dry_run,
        )
    except (TelegramControlOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote telegram control dispatch to {result.output_path}")
    if result.exit_code != 0:
        print(f"telegram control dispatch {result.status.lower()}", file=sys.stderr)
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
        ai_value_report=args.ai_value_report,
        require_ai_evidence=args.require_ai_evidence,
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


def _live_safe_flatten(args: argparse.Namespace) -> int:
    try:
        broker = _FixtureLivePositionBroker(_load_live_positions_fixture(args.positions_fixture))
        result = run_live_safe_flatten(
            as_of_date=args.as_of_date,
            broker=broker,
            allowlist=args.allowlist,
            reviewer=args.reviewer,
            reason=args.reason,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote live safe flatten evidence to {result.output_path}")
    print(f"wrote live safe flatten markdown to {result.markdown_path}")
    if result.status == "BLOCKED":
        print("live safe flatten blocked", file=sys.stderr)
    return result.exit_code


class _FixtureLivePositionBroker:
    def __init__(self, positions: list[LivePosition]) -> None:
        self._positions = positions

    def read_positions(self) -> list[LivePosition]:
        return self._positions


def _load_live_positions_fixture(path: str | Path) -> list[LivePosition]:
    payload = read_json_artifact(path)
    raw_positions = payload.get("positions")
    if not isinstance(raw_positions, list):
        raise ValueError("positions fixture must contain a positions list")

    positions: list[LivePosition] = []
    for index, item in enumerate(raw_positions):
        if not isinstance(item, dict):
            raise ValueError(f"positions fixture item {index} must be an object")
        symbol = item.get("symbol")
        quantity = item.get("quantity")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(f"positions fixture item {index} missing symbol")
        try:
            parsed_quantity = float(quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"positions fixture item {index} has invalid quantity") from exc
        positions.append(LivePosition(symbol=symbol.upper(), quantity=parsed_quantity))
    return positions


def _live_canary(args: argparse.Namespace) -> int:
    try:
        risk_limits = None
        allowlist = None
        runtime_factory = None
        if args.enable_real_submit:
            if not args.risk_live:
                raise ValueError("--risk-live is required with --enable-real-submit")
            if args.reference_price is None:
                raise ValueError("--reference-price is required with --enable-real-submit")
            if not args.confirm_real_submit:
                raise ValueError("--confirm-real-submit is required with --enable-real-submit")
            universe = load_universe_config(args.universe)
            risk_limits = load_risk_config(args.risk_live, allow_live=True)
            allowlist = universe.symbols

            def runtime_factory():
                runtime = build_alpaca_live_runtime()
                broker = AlpacaLiveBroker(
                    client=runtime.trading_client,
                    allowlist=universe.symbols,
                    risk_limits=risk_limits,
                    submit_enabled=True,
                )
                return {
                    "broker": broker,
                    "market_clock": runtime.market_clock,
                    "live_price_result": runtime.live_price_result,
                    "live_price": runtime.live_price,
                    "credentials_read": runtime.credentials_read,
                }

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
            confirm_real_submit=args.confirm_real_submit,
            reference_price=args.reference_price,
            risk_limits=risk_limits,
            allowlist=allowlist,
            runtime_factory=runtime_factory,
            autonomy_state_dir=args.autonomy_state_dir,
            autonomy_market=args.autonomy_market,
            signal_plan=args.signal_plan,
            approval_registry_dir=args.approval_registry_dir,
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


def _cross_asset_session_plan(args: argparse.Namespace) -> int:
    try:
        result = run_cross_asset_session_plan(
            as_of_date=args.as_of_date,
            positions=args.positions,
            current_time=args.current_time,
            futures_readiness=args.futures_readiness,
            forex_readiness=args.forex_readiness,
            futures_session_close_time=args.futures_session_close_time,
            forex_weekend_close_time=args.forex_weekend_close_time,
            flatten_window_minutes=args.flatten_window_minutes,
            longer_term_symbols=args.longer_term_symbol,
            output=args.output,
            markdown_output=args.markdown_output,
            ledger_output=args.ledger_output,
        )
    except (CrossAssetSessionPlanOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote cross-asset session plan to {result.output_path}")
    print(f"wrote cross-asset session plan markdown to {result.markdown_path}")
    if result.exit_code != 0:
        print(f"cross-asset session plan {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _forex_readiness_report(args: argparse.Namespace) -> int:
    try:
        result = run_forex_readiness_report(
            config=args.config,
            output=args.output,
            markdown_output=args.markdown_output,
        )
    except (ConfigError, ForexReadinessOperationalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote Forex readiness report to {result.output_path}")
    print(f"wrote Forex readiness markdown to {result.markdown_path}")
    if result.status == "BLOCKED":
        print("Forex readiness blocked", file=sys.stderr)
    return result.exit_code


def _autonomy_status(args: argparse.Namespace) -> int:
    state = load_autonomy_state(args.market, state_dir=args.state_dir)
    output_path = Path(args.output) if args.output else Path(args.state_dir) / args.market / "status_latest.json"
    write_json_artifact(state.to_dict(), output_path)
    print(
        f"autonomy state for {args.market}: level={state.level} "
        f"fail_closed={state.fail_closed} open_incident={state.open_incident}"
    )
    print(f"wrote autonomy status to {output_path}")
    if state.fail_closed:
        print("autonomy state is fail-closed", file=sys.stderr)
        return 1
    return 0


def _autonomy_certify(args: argparse.Namespace) -> int:
    evidence = {
        "clean_days": args.clean_days,
        "evidence_kind": args.evidence_kind,
        "artifact_hash": args.artifact_hash,
    }
    decision = certify_autonomy_promotion(
        market=args.market,
        target_level=args.target_level,
        evidence=evidence,
        reviewer=args.reviewer,
        reason=args.reason,
        state_dir=args.state_dir,
        output=args.output,
    )
    print(f"wrote autonomy certification decision to {decision.output_path}")
    if decision.status != "OK":
        print(f"autonomy certification {decision.status.lower()}", file=sys.stderr)
    return decision.exit_code


def _autonomy_incident(args: argparse.Namespace) -> int:
    decision = record_autonomy_incident(
        market=args.market,
        severity=args.severity,
        source=args.source,
        reason=args.reason,
        state_dir=args.state_dir,
        output=args.output,
    )
    print(f"wrote autonomy incident decision to {decision.output_path}")
    if decision.status not in ("OK",):
        print(f"autonomy incident recorded as {decision.status.lower()}", file=sys.stderr)
    return decision.exit_code


def _autonomy_resolve_incident(args: argparse.Namespace) -> int:
    decision = resolve_autonomy_incident(
        market=args.market,
        reviewer=args.reviewer,
        reason=args.reason,
        state_dir=args.state_dir,
    )
    print(f"wrote autonomy incident resolution decision to {decision.output_path}")
    if decision.status != "OK":
        print(f"autonomy incident resolution {decision.status.lower()}", file=sys.stderr)
    return decision.exit_code


def _autonomy_incident_sync(args: argparse.Namespace) -> int:
    try:
        result = run_autonomy_incident_sync(
            as_of_date=args.as_of_date,
            market=args.market,
            breaker_state=args.breaker_state,
            reconciliation_report=args.reconciliation_report,
            risk_state=args.risk_state,
            autonomy_state_dir=args.autonomy_state_dir,
            output_dir=args.output_dir,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote autonomy incident sync report to {result.output_path}")
    if result.status != "OK":
        print(f"autonomy incident sync {result.status.lower()}", file=sys.stderr)
    return result.exit_code


def _paper_n0_certification(args: argparse.Namespace) -> int:
    try:
        result = run_paper_n0_certification(
            as_of_date=args.as_of_date,
            market=args.market,
            session_ledgers=args.session_ledger,
            performance_report=args.performance_report,
            min_clean_days=args.min_clean_days,
            max_drawdown_pct=args.max_drawdown_pct,
            output_dir=args.output_dir,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote N0 certification report to {result.output_path}")
    if result.status != "CERTIFIED_READY":
        print(f"N0 certification {result.status.lower()}", file=sys.stderr)
    return result.exit_code


_SIGNAL_APPROVAL_GATE_ACTIONS = (
    "paper_auto",
    "real_submit_approved",
    "real_submit_veto_window",
    "real_submit_auto",
)


def _paper_signal_approval_status(args: argparse.Namespace) -> int:
    registry = load_signal_approval_registry(args.as_of_date, registry_dir=args.registry_dir)
    now = datetime.now(UTC).isoformat()
    plan_report: dict[str, object] | None = None
    if args.plan:
        try:
            plan_payload = read_json_artifact(args.plan)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        plan_hash = compute_plan_hash(plan_payload)
        plan_generated_at = str(plan_payload.get("generated_at") or now)
        gates = {
            action: evaluate_signal_approval_gate(
                plan_hash=plan_hash,
                registry_payload=registry,
                requested_action=action,
                plan_generated_at=plan_generated_at,
                now=now,
            )
            for action in _SIGNAL_APPROVAL_GATE_ACTIONS
        }
        plan_report = {
            "plan_path": str(Path(args.plan)),
            "plan_hash": plan_hash,
            "plan_as_of_date": plan_payload.get("as_of_date"),
            "plan_generated_at": plan_generated_at,
            "gate": gates,
        }

    payload = {
        "schema_version": "1.0",
        "generated_at": now,
        "as_of_date": args.as_of_date,
        "registry_dir": str(Path(args.registry_dir)),
        "fail_closed": bool(registry.get("fail_closed")),
        "record_count": len(registry.get("records") or []),
        "records": registry.get("records"),
        "plan": plan_report,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }
    output_path = (
        Path(args.output)
        if args.output
        else Path(args.registry_dir) / args.as_of_date / "status_latest.json"
    )
    write_json_artifact(payload, output_path)
    print(
        f"signal approval status for {args.as_of_date}: records={payload['record_count']} "
        f"fail_closed={payload['fail_closed']}"
    )
    print(f"wrote signal approval status to {output_path}")
    if registry.get("fail_closed"):
        print("signal approval registry is fail-closed", file=sys.stderr)
        return 1
    return 0


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
    if args.model not in ("logistic-baseline", "lightgbm-baseline"):
        print(
            "supported models: logistic-baseline, lightgbm-baseline "
            "(lightgbm-baseline requires the 'ml' optional extras)",
            file=sys.stderr,
        )
        return 2
    records = read_records(args.dataset)
    manifest = build_dataset_manifest(records, source=str(args.dataset))
    explicit_feature_names = _parse_feature_names(args.feature_names)
    if explicit_feature_names is None:
        feature_names = _default_feature_names(records)
        feature_source = "default"
    else:
        if not explicit_feature_names:
            print("--feature-names must list at least one non-empty name", file=sys.stderr)
            return 2
        invalid = [name for name in explicit_feature_names if not has_finite_feature_value(records, name)]
        if invalid:
            print(
                "--feature-names: the following names have no finite values in the dataset: "
                + ", ".join(invalid),
                file=sys.stderr,
            )
            return 2
        feature_names = explicit_feature_names
        feature_source = "explicit"
    standardize = bool(getattr(args, "standardize_features", False))
    if args.model == "lightgbm-baseline" and standardize:
        print(
            "--standardize-features does not apply to lightgbm-baseline "
            "(tree models are scale-invariant); omit it",
            file=sys.stderr,
        )
        return 2
    labeling = args.labeling
    # Default (direction) keeps today's byte-identical pipeline. Triple-barrier
    # labels are overlapping (each example's label depends on close_{i+horizon}),
    # so we MUST purge ``horizon`` examples at the train/test boundary and at
    # each walk-forward window — otherwise the trainer would be allowed to
    # memorize the labels it is then asked to predict.
    if labeling == "triple_barrier":
        try:
            examples = build_triple_barrier_examples(
                records,
                feature_names=feature_names,
                horizon=args.label_horizon,
                atr_mult=args.label_atr_mult,
                vol_column=args.vol_column,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        embargo = args.label_horizon
    else:
        examples = build_supervised_examples(records, feature_names=feature_names)
        embargo = 0
    config = LogisticBaselineConfig(feature_names=feature_names)
    split = temporal_train_test_split(examples, test_fraction=config.test_fraction, embargo=embargo)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.model == "lightgbm-baseline":
        # Non-linear baseline (Sprint I1). Trees are scale-invariant so no
        # standardization is applied; the same feature/labeling/embargo pipeline
        # feeds a per-window LightGBM fit via walk_forward_evaluate's train_fn.
        # The fitted booster is not JSON-serializable, so we persist a descriptor
        # (not a logistic model) — evidence lives in the run artifact's metrics.
        try:
            from trading_ai.models.baseline import (  # noqa: PLC0415
                LightGBMBaselineConfig,
                train_lightgbm_baseline,
            )

            lgb_config = LightGBMBaselineConfig(feature_names=feature_names)

            def _fit_lgb(rows):
                return train_lightgbm_baseline(rows, lgb_config)

            model = _fit_lgb(split.train)
        except ImportError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        output.write_text(
            json.dumps(
                {
                    "model_type": args.model,
                    "feature_names": list(feature_names),
                    "note": (
                        "LightGBM booster is not JSON-serializable; this file is a "
                        "descriptor. Metrics live in the run artifact."
                    ),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        walk_forward = walk_forward_evaluate(
            examples,
            config,
            min_train_size=max(2, len(split.train) // 2),
            test_size=max(1, len(split.test)),
            embargo=embargo,
            train_fn=_fit_lgb,
        )
    else:
        # NOTE: stats computed ONLY on the TRAIN split (anti-leakage) — the hold-out
        # test set never influences the scaler. Same flag propagates to walk-forward
        # so each of its windows computes its own stats from that window's train rows.
        model = train_logistic_baseline(split.train, config, standardize=standardize)
        save_model(model, str(output))
        walk_forward = walk_forward_evaluate(
            examples,
            config,
            min_train_size=max(2, len(split.train) // 2),
            test_size=max(1, len(split.test)),
            embargo=embargo,
            standardize=standardize,
        )
    run_payload = {
        "model_type": args.model,
        "model_path": str(output),
        "dataset_path": str(Path(args.dataset)),
        "dataset_hash": manifest["dataset_hash"],
        "feature_names": list(config.feature_names),
        "feature_source": feature_source,
        "standardized": standardize,
        "labeling": labeling,
        "train_range": [split.train[0].timestamp, split.train[-1].timestamp],
        "test_range": [split.test[0].timestamp, split.test[-1].timestamp],
        "metrics": {
            "train": evaluate_classifier(model, split.train),
            "test": evaluate_classifier(model, split.test),
            "walk_forward": walk_forward,
        },
    }
    # In direction mode we deliberately do NOT add the triple-barrier-only
    # metadata so the default-path run payload stays a strict subset of the
    # pre-H2 schema (legacy hashes / downstream assertions stay stable).
    if labeling == "triple_barrier":
        train_positive_count = sum(1 for ex in split.train if ex.target == 1)
        train_positive_rate = (
            train_positive_count / len(split.train) if split.train else 0.0
        )
        run_payload["label_horizon"] = args.label_horizon
        run_payload["label_atr_mult"] = args.label_atr_mult
        run_payload["vol_column"] = args.vol_column
        run_payload["label_positive_rate"] = train_positive_rate
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


def _llm_local_runtime(args: argparse.Namespace) -> int:
    try:
        result = run_llm_local_runtime(
            device_root=args.device_root,
            output=args.output,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote local LLM runtime report to {result.output_path}")
    if result.status != "CUDA_AVAILABLE":
        print("local LLM runtime using CPU fallback", file=sys.stderr)
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
    payload: dict[str, object] = {
        "timestamp": signal.timestamp,
        "symbol": signal.symbol,
        "probability": signal.probability,
        "threshold": signal.threshold,
        "action": signal.action,
    }
    if signal.policy_action is not None:
        payload["policy_action"] = signal.policy_action
    if signal.reason_codes:
        payload["reason_codes"] = list(signal.reason_codes)
    if signal.model_id is not None:
        payload["model_id"] = signal.model_id
    if signal.open_score is not None:
        payload["open_score"] = signal.open_score
    if signal.close_score is not None:
        payload["close_score"] = signal.close_score
    if signal.risk_inputs is not None:
        payload["risk_inputs"] = dict(signal.risk_inputs)
    if signal.safety is not None:
        payload["safety"] = dict(signal.safety)
    return payload


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
