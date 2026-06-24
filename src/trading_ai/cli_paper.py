"""Paper-trading CLI parser wiring."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

CliHandler = Callable[[argparse.Namespace], int]


@dataclass(frozen=True)
class PaperCliHandlers:
    paper: CliHandler
    paper_audit: CliHandler
    paper_session: CliHandler
    paper_execute_session: CliHandler
    paper_position_watch: CliHandler
    paper_safe_flatten: CliHandler
    paper_close_session: CliHandler
    paper_observability: CliHandler
    paper_monitor: CliHandler
    paper_campaign_report: CliHandler
    paper_day_close: CliHandler
    paper_performance_report: CliHandler
    paper_statement_validate: CliHandler
    paper_weekly_summary: CliHandler
    paper_operator_status: CliHandler
    paper_strategy_quality: CliHandler
    paper_phase_review_report: CliHandler
    paper_trial_day: CliHandler
    paper_ops_check: CliHandler
    paper_ops_rehearsal: CliHandler
    paper_evidence_index: CliHandler
    paper_daily: CliHandler
    paper_daily_from_readiness: CliHandler
    prepare_paper_daily: CliHandler
    llm_paper_review: CliHandler
    llm_signal_proposals: CliHandler
    paper_signal_arbitration: CliHandler
    paper_challenger_shadow_plan: CliHandler
    paper_challenger_signals: CliHandler
    paper_shadow_outcome_report: CliHandler
    paper_shadow_scorecard: CliHandler
    paper_model_alias_decision: CliHandler
    paper_autopilot_plan: CliHandler
    paper_review_decision: CliHandler
    paper_bot_cycle: CliHandler
    paper_auto_cycle: CliHandler
    llm_context_pack: CliHandler


def add_paper_subcommands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    handlers: PaperCliHandlers,
    paper_daily_default_config: str,
) -> None:
    paper = subparsers.add_parser("paper")
    paper.add_argument("--broker", default="alpaca")
    paper_mode = paper.add_mutually_exclusive_group()
    paper_mode.add_argument("--dry-run", action="store_true", default=True)
    paper_mode.add_argument("--real-paper", action="store_true")
    paper.add_argument("--confirm-paper", action="store_true")
    paper.add_argument("--universe", default="configs/universe.yml")
    paper.add_argument("--risk", default="configs/risk.yml")
    paper.add_argument("--read-account", action="store_true")
    paper.add_argument("--read-positions", action="store_true")
    paper.add_argument("--kill-switch-test", action="store_true")
    paper.add_argument("--signal-model", default="models/latest_model.json")
    paper.add_argument("--features", default="data/processed/features.csv")
    paper.add_argument("--signal-threshold", type=float, default=0.5)
    paper.add_argument("--submit-signal-order", action="store_true")
    paper.add_argument("--max-feature-age-days", type=int, default=5)
    paper.add_argument("--as-of-date")
    paper.add_argument("--list-orders", action="store_true")
    paper.add_argument("--order-status", default="open")
    paper.add_argument("--get-order", action="store_true")
    paper.add_argument("--order-id")
    paper.add_argument("--client-order-id")
    paper.add_argument("--cancel-order", action="store_true")
    paper.add_argument("--confirm-cancel", action="store_true")
    paper.add_argument("--reconcile-order", action="store_true")
    paper.add_argument("--source-report")
    paper.add_argument("--ledger-output")
    paper.add_argument("--output", default="reports/tmp/paper/latest.json")
    paper.set_defaults(func=handlers.paper)

    paper_audit = subparsers.add_parser("paper-audit")
    paper_audit.add_argument("--freshness-report", required=True)
    paper_audit.add_argument("--signal-report", required=True)
    paper_audit.add_argument("--reconciliation-report")
    paper_audit.add_argument("--backtest-report")
    paper_audit.add_argument("--promotion-report")
    paper_audit.add_argument("--drift-report")
    paper_audit.add_argument("--mlflow-candidate-review-report")
    paper_audit.add_argument("--paper-graduation-report")
    paper_audit.add_argument("--output", default="reports/tmp/paper_audit/latest.json")
    paper_audit.add_argument("--markdown-output", default="reports/tmp/paper_audit/latest.md")
    paper_audit.add_argument("--as-of-date", default="today")
    paper_audit.set_defaults(func=handlers.paper_audit)

    paper_session = subparsers.add_parser("paper-session")
    paper_session.add_argument("--source-csv", "--source", dest="source_csv", required=True)
    paper_session.add_argument("--from", dest="start", required=True)
    paper_session.add_argument("--to", dest="end", required=True)
    paper_session.add_argument("--reference-features")
    paper_session.add_argument("--output-dir", default="reports/tmp/paper_session/latest")
    paper_session.add_argument("--config", default="configs/universe.yml")
    paper_session.add_argument("--risk", default="configs/risk.yml")
    paper_session.add_argument("--signal-model", default="models/latest_model.json")
    paper_session.add_argument("--as-of-date", default="today")
    paper_session.add_argument("--signal-threshold", type=float, default=0.5)
    paper_session.add_argument("--max-age-days", type=int, default=5)
    paper_session.add_argument("--max-feature-age-days", type=int, default=5)
    paper_session.add_argument("--backtest-report")
    paper_session.add_argument("--promotion-report")
    paper_session.add_argument("--reconciliation-report")
    paper_session.add_argument("--campaign-report")
    paper_session.add_argument("--phase-review")
    paper_session.add_argument("--review-mlflow-paper-candidate", action="store_true")
    paper_session.add_argument("--mlflow-registry-dir", default="reports/registry")
    paper_session.add_argument("--mlflow-tracking-uri", default="reports/mlruns")
    paper_session.add_argument(
        "--mlflow-registered-model-name",
        default="approved-data-logistic-baseline",
    )
    paper_session.add_argument("--mlflow-alias", default="paper-candidate")
    paper_session.add_argument("--ledger-output")
    paper_session.set_defaults(func=handlers.paper_session)

    paper_execute = subparsers.add_parser("paper-execute-session")
    paper_execute.add_argument("--session-dir", required=True)
    paper_execute.add_argument("--confirm-paper", action="store_true")
    paper_execute.add_argument("--confirm-submit", action="store_true")
    paper_execute.add_argument("--confirm-dynamic-position-actions", action="store_true")
    paper_execute.add_argument("--output-dir")
    paper_execute.add_argument("--as-of-date", default="today")
    paper_execute.add_argument("--max-feature-age-days", type=int, default=5)
    paper_execute.add_argument("--risk-state-path", default="reports/tmp/paper_risk_state.json")
    paper_execute.add_argument("--ledger-output")
    paper_execute.set_defaults(func=handlers.paper_execute_session)

    paper_position_watch = subparsers.add_parser("paper-position-watch")
    paper_position_watch.add_argument("--session-dir", required=True)
    paper_position_watch.add_argument("--confirm-paper", action="store_true")
    paper_position_watch.add_argument("--confirm-dynamic-position-actions", action="store_true")
    paper_position_watch.add_argument("--as-of-date", default="today")
    paper_position_watch.add_argument("--output", default="reports/tmp/paper_position_watch/latest.json")
    paper_position_watch.add_argument("--markdown-output", default="reports/tmp/paper_position_watch/latest.md")
    paper_position_watch.set_defaults(func=handlers.paper_position_watch)

    paper_safe_flatten = subparsers.add_parser("paper-safe-flatten")
    paper_safe_flatten.add_argument("--universe", default="configs/universe.yml")
    paper_safe_flatten.add_argument("--risk", default="configs/risk.yml")
    paper_safe_flatten.add_argument("--confirm-paper", action="store_true")
    paper_safe_flatten.add_argument("--confirm-flatten", action="store_true")
    paper_safe_flatten.add_argument("--reset-kill-switch-after", action="store_true")
    paper_safe_flatten.add_argument("--as-of-date", default="today")
    paper_safe_flatten.add_argument("--risk-state-path", default="reports/tmp/paper_risk_state.json")
    paper_safe_flatten.add_argument("--output", default="reports/tmp/paper_safe_flatten/latest.json")
    paper_safe_flatten.add_argument("--markdown-output", default="reports/tmp/paper_safe_flatten/latest.md")
    paper_safe_flatten.set_defaults(func=handlers.paper_safe_flatten)

    paper_close = subparsers.add_parser("paper-close-session")
    paper_close.add_argument("--session-dir", required=True)
    paper_close.add_argument("--confirm-paper", action="store_true")
    paper_close.add_argument("--execution-report")
    paper_close.add_argument("--output-dir")
    paper_close.add_argument("--ledger-output")
    paper_close.set_defaults(func=handlers.paper_close_session)

    paper_observability = subparsers.add_parser("paper-observability")
    paper_observability.add_argument("--sessions-root", default="reports/tmp/paper_session")
    paper_observability.add_argument("--session-dir", action="append", default=[])
    paper_observability.add_argument("--ledger-input", action="append", default=[])
    paper_observability.add_argument("--output", default="reports/tmp/paper_observability/latest.json")
    paper_observability.add_argument("--markdown-output", default="reports/tmp/paper_observability/latest.md")
    paper_observability.set_defaults(func=handlers.paper_observability)

    paper_monitor = subparsers.add_parser("paper-monitor")
    paper_monitor.add_argument("--sessions-root", default="reports/tmp/paper_session")
    paper_monitor.add_argument("--session-dir", action="append", default=[])
    paper_monitor.add_argument("--ledger-input", action="append", default=[])
    paper_monitor.add_argument("--output", default="reports/tmp/paper_monitor/latest.json")
    paper_monitor.add_argument("--markdown-output", default="reports/tmp/paper_monitor/latest.md")
    paper_monitor.add_argument("--as-of-date", default="today")
    paper_monitor.add_argument("--min-stable-sessions", type=int, default=60)
    paper_monitor.add_argument("--broker-read-only", action="store_true")
    paper_monitor.add_argument("--confirm-paper", action="store_true")
    paper_monitor.add_argument("--universe", default="configs/universe.yml")
    paper_monitor.add_argument("--risk", default="configs/risk.yml")
    paper_monitor.add_argument("--order-status", default="open")
    paper_monitor.add_argument("--ledger-output")
    paper_monitor.add_argument("--send-telegram", action="store_true")
    paper_monitor.add_argument("--telegram-dry-run", action="store_true")
    paper_monitor.add_argument("--telegram-send-warnings", action="store_true")
    paper_monitor.set_defaults(func=handlers.paper_monitor)

    paper_campaign = subparsers.add_parser("paper-campaign-report")
    paper_campaign.add_argument("--sessions-root", default="reports/tmp/paper_session")
    paper_campaign.add_argument("--readiness-root", default="reports/tmp/paper_daily_prepare")
    paper_campaign.add_argument("--decisions-root", default="reports/tmp/paper_decisions")
    paper_campaign.add_argument("--performance-root", default="reports/tmp/paper_performance")
    paper_campaign.add_argument("--trial-day-root", default="reports/tmp/paper_trial_day")
    paper_campaign.add_argument("--risk", default="configs/risk.yml")
    paper_campaign.add_argument("--ledger-input", action="append", default=[])
    paper_campaign.add_argument("--min-paper-auto-clean-sessions", type=int, default=20)
    paper_campaign.add_argument("--min-stable-sessions", type=int, default=60)
    paper_campaign.add_argument("--min-trial-days", type=int, default=30)
    paper_campaign.add_argument("--output", default="reports/tmp/paper_campaign/latest.json")
    paper_campaign.add_argument("--markdown-output", default="reports/tmp/paper_campaign/latest.md")
    paper_campaign.add_argument("--as-of-date", default="today")
    paper_campaign.set_defaults(func=handlers.paper_campaign_report)

    paper_day_close = subparsers.add_parser("paper-day-close")
    paper_day_close.add_argument("--readiness", required=True)
    paper_day_close.add_argument("--broker-run", required=True)
    paper_day_close.add_argument("--monitor", required=True)
    paper_day_close.add_argument("--campaign-report", required=True)
    paper_day_close.add_argument("--output-dir", default="reports/tmp/paper_decisions")
    paper_day_close.add_argument("--as-of-date", default="auto")
    paper_day_close.add_argument("--operator")
    paper_day_close.add_argument("--reason")
    paper_day_close.add_argument("--ledger-output")
    paper_day_close.set_defaults(func=handlers.paper_day_close)

    paper_performance = subparsers.add_parser("paper-performance-report")
    paper_performance.add_argument("--sessions-root", default="reports/tmp/paper_session")
    paper_performance.add_argument("--session-dir", action="append", default=[])
    paper_performance.add_argument("--ledger-input", action="append", default=[])
    paper_performance.add_argument("--backtest-report")
    paper_performance.add_argument("--broker-statement")
    paper_performance.add_argument("--min-stable-sessions", type=int, default=60)
    paper_performance.add_argument("--min-stable-fills", type=int, default=60)
    paper_performance.add_argument("--output", default="reports/tmp/paper_performance/latest.json")
    paper_performance.add_argument("--markdown-output", default="reports/tmp/paper_performance/latest.md")
    paper_performance.set_defaults(func=handlers.paper_performance_report)

    paper_statement = subparsers.add_parser("paper-statement-validate")
    paper_statement.add_argument("--statement", required=True)
    paper_statement.add_argument("--as-of-date", required=True)
    paper_statement.add_argument("--output-dir", default="reports/tmp/paper_statements")
    paper_statement.set_defaults(func=handlers.paper_statement_validate)

    paper_weekly = subparsers.add_parser("paper-weekly-summary")
    paper_weekly.add_argument("--decisions-root", default="reports/tmp/paper_decisions")
    paper_weekly.add_argument("--performance-root", default="reports/tmp/paper_performance")
    paper_weekly.add_argument("--campaign-root", default="reports/tmp/paper_campaign")
    paper_weekly.add_argument("--ledger-input", action="append", default=[])
    paper_weekly.add_argument("--output-dir", default="reports/tmp/paper_weekly_summary")
    paper_weekly.add_argument("--week", default="auto")
    paper_weekly.add_argument("--as-of-date", default="today")
    paper_weekly.add_argument("--history-weeks", type=int, default=1)
    paper_weekly.set_defaults(func=handlers.paper_weekly_summary)

    paper_operator = subparsers.add_parser("paper-operator-status")
    paper_operator.add_argument("--as-of-date", required=True)
    paper_operator.add_argument("--cycle-root", default="reports/tmp/paper_auto_cycle")
    paper_operator.add_argument("--ledger", default="reports/tmp/paper_auto_cycle/session_ledger.jsonl")
    paper_operator.add_argument("--monitor")
    paper_operator.add_argument("--performance")
    paper_operator.add_argument("--lock-dir")
    paper_operator.add_argument("--max-lock-age-minutes", type=int, default=90)
    paper_operator.add_argument("--output-dir", default="reports/tmp/paper_operator_status")
    paper_operator.set_defaults(func=handlers.paper_operator_status)

    paper_strategy_quality = subparsers.add_parser("paper-strategy-quality")
    paper_strategy_quality.add_argument("--as-of-date", required=True)
    paper_strategy_quality.add_argument("--model-signals", required=True)
    paper_strategy_quality.add_argument("--signal-plan", required=True)
    paper_strategy_quality.add_argument("--performance", required=True)
    paper_strategy_quality.add_argument("--challenger-report")
    paper_strategy_quality.add_argument("--ledger-input", action="append", default=[])
    paper_strategy_quality.add_argument("--lookback-sessions", type=int, default=60)
    paper_strategy_quality.add_argument("--min-clean-sessions", type=int, default=20)
    paper_strategy_quality.add_argument("--min-paper-fills", type=int, default=20)
    paper_strategy_quality.add_argument("--max-cost-drag-bps", type=float)
    paper_strategy_quality.add_argument("--max-trade-count-gap-pct", type=float)
    paper_strategy_quality.add_argument("--max-blocker-rate-pct", type=float)
    paper_strategy_quality.add_argument("--max-llm-disagreement-rate-pct", type=float)
    paper_strategy_quality.add_argument("--output-dir", default="reports/tmp/paper_strategy_quality")
    paper_strategy_quality.set_defaults(func=handlers.paper_strategy_quality)

    paper_phase_review = subparsers.add_parser("paper-phase-review-report")
    paper_phase_review.add_argument("--as-of-date", required=True)
    paper_phase_review.add_argument("--campaign-report", required=True)
    paper_phase_review.add_argument("--risk", default="configs/risk.yml")
    paper_phase_review.add_argument("--performance-report", required=True)
    paper_phase_review.add_argument("--operator-status", required=True)
    paper_phase_review.add_argument("--strategy-quality", required=True)
    paper_phase_review.add_argument("--evidence-index", required=True)
    paper_phase_review.add_argument("--weekly-summary")
    paper_phase_review.add_argument("--trial-day-root")
    paper_phase_review.add_argument("--min-stable-sessions", type=int, default=60)
    paper_phase_review.add_argument("--output-dir", default="reports/tmp/paper_phase_review")
    paper_phase_review.set_defaults(func=handlers.paper_phase_review_report)

    paper_trial_day = subparsers.add_parser("paper-trial-day")
    paper_trial_day.add_argument("--as-of-date", required=True)
    paper_trial_day.add_argument("--cycle", required=True)
    paper_trial_day.add_argument("--monitor", required=True)
    paper_trial_day.add_argument("--performance", required=True)
    paper_trial_day.add_argument("--shadow-outcome", required=True)
    paper_trial_day.add_argument("--risk", default="configs/risk.yml")
    paper_trial_day.add_argument("--output-dir", default="reports/tmp/paper_trial_day")
    paper_trial_day.set_defaults(func=handlers.paper_trial_day)

    paper_ops = subparsers.add_parser("paper-ops-check")
    paper_ops.add_argument("--as-of-date", required=True)
    paper_ops.add_argument("--readiness-root", default="reports/tmp/paper_daily_prepare")
    paper_ops.add_argument("--sessions-root", default="reports/tmp/paper_session")
    paper_ops.add_argument("--monitor-root", default="reports/tmp/paper_monitor")
    paper_ops.add_argument("--campaign-root", default="reports/tmp/paper_campaign")
    paper_ops.add_argument("--decisions-root", default="reports/tmp/paper_decisions")
    paper_ops.add_argument("--performance-root", default="reports/tmp/paper_performance")
    paper_ops.add_argument("--ledger-input", action="append", default=[])
    paper_ops.add_argument("--output-dir", default="reports/tmp/paper_ops_check")
    paper_ops.set_defaults(func=handlers.paper_ops_check)

    paper_rehearsal = subparsers.add_parser("paper-ops-rehearsal")
    paper_rehearsal.add_argument("--as-of-date", required=True)
    paper_rehearsal.add_argument(
        "--scenario",
        default="complete",
        choices=(
            "complete",
            "missing-performance",
            "stop",
            "invalid-statement",
            "open-order",
            "existing-position",
            "stale-dataset",
            "statement-mismatch",
            "fill-unreconciled",
            "malicious-llm-context",
            "59-stable-sessions",
            "60-stable-ready",
            "duplicate-cycle",
            "stale-lock",
            "corrupt-ledger",
            "quality-blocked",
            "phase-not-ready",
            "retrain-due",
            "not-due",
            "duplicate-retrain",
            "candidate-rejected",
            "drift-blocked",
            "shadow-insufficient",
            "shadow-ready",
            "alias-approved",
            "alias-blocked",
            "alias-expired",
            "challenger-underperforms",
            "malicious-alias-llm",
            "alias-invalid-model",
            "malicious-adaptive-llm",
        ),
    )
    paper_rehearsal.add_argument("--output-dir", default="reports/tmp/paper_rehearsal")
    paper_rehearsal.set_defaults(func=handlers.paper_ops_rehearsal)

    paper_evidence = subparsers.add_parser("paper-evidence-index")
    paper_evidence.add_argument("--as-of-date", required=True)
    paper_evidence.add_argument("--readiness-root", default="reports/tmp/paper_daily_prepare")
    paper_evidence.add_argument("--monitor-root", default="reports/tmp/paper_monitor")
    paper_evidence.add_argument("--campaign-root", default="reports/tmp/paper_campaign")
    paper_evidence.add_argument("--decisions-root", default="reports/tmp/paper_decisions")
    paper_evidence.add_argument("--performance-root", default="reports/tmp/paper_performance")
    paper_evidence.add_argument("--ops-root", default="reports/tmp/paper_ops_check")
    paper_evidence.add_argument("--weekly-root", default="reports/tmp/paper_weekly_summary")
    paper_evidence.add_argument("--statement-root", default="reports/tmp/paper_statements")
    paper_evidence.add_argument("--challenger-decisions-root", default="reports/tmp/model_challenger_decisions")
    paper_evidence.add_argument("--output-dir", default="reports/tmp/paper_evidence_index")
    paper_evidence.set_defaults(func=handlers.paper_evidence_index)

    llm_paper_review = subparsers.add_parser("llm-paper-review")
    llm_paper_review.add_argument("--as-of-date", required=True)
    llm_paper_review.add_argument("--readiness", required=True)
    llm_paper_review.add_argument("--ops-check", required=True)
    llm_paper_review.add_argument("--evidence-index", required=True)
    llm_paper_review.add_argument("--performance")
    llm_paper_review.add_argument("--challenger-report")
    llm_paper_review.add_argument("--shadow-scorecard")
    llm_paper_review.add_argument("--paper-model-alias")
    llm_paper_review.add_argument("--llm-model-alias")
    llm_paper_review.add_argument("--cycle-report")
    llm_paper_review.add_argument("--output-dir", default="reports/tmp/llm_paper_review")
    llm_paper_review.add_argument("--use-openai", action="store_true")
    llm_paper_review.add_argument("--confirm-llm", action="store_true")
    llm_paper_review.add_argument("--model")
    llm_paper_review.set_defaults(func=handlers.llm_paper_review)

    llm_signal_proposals = subparsers.add_parser("llm-signal-proposals")
    llm_signal_proposals.add_argument("--as-of-date", required=True)
    llm_signal_proposals.add_argument("--readiness", required=True)
    llm_signal_proposals.add_argument("--features", required=True)
    llm_signal_proposals.add_argument("--model-signals", required=True)
    llm_signal_proposals.add_argument("--context-digest")
    llm_signal_proposals.add_argument("--llm-model-alias")
    llm_signal_proposals.add_argument("--output-dir", default="reports/tmp/llm_signal_proposals")
    llm_signal_proposals.add_argument("--use-openai", action="store_true")
    llm_signal_proposals.add_argument("--confirm-llm", action="store_true")
    llm_signal_proposals.add_argument("--model")
    llm_signal_proposals.set_defaults(func=handlers.llm_signal_proposals)

    llm_context_pack = subparsers.add_parser("llm-context-pack")
    llm_context_pack.add_argument("--as-of-date", required=True)
    llm_context_pack.add_argument("--cycle-root", default="reports/tmp/paper_auto_cycle")
    llm_context_pack.add_argument("--campaign-status")
    llm_context_pack.add_argument("--performance-report")
    llm_context_pack.add_argument("--phase-review")
    llm_context_pack.add_argument("--training-cycle")
    llm_context_pack.add_argument("--challenger-report")
    llm_context_pack.add_argument("--shadow-plan")
    llm_context_pack.add_argument("--shadow-scorecard")
    llm_context_pack.add_argument("--paper-model-alias")
    llm_context_pack.add_argument("--llm-model-alias")
    llm_context_pack.add_argument("--evidence-index")
    llm_context_pack.add_argument("--weekly-summary")
    llm_context_pack.add_argument("--operator-status", required=True)
    llm_context_pack.add_argument("--quality-report", required=True)
    llm_context_pack.add_argument("--output-dir", default="reports/tmp/llm_context_pack")
    llm_context_pack.set_defaults(func=handlers.llm_context_pack)

    paper_signal_arbitration = subparsers.add_parser("paper-signal-arbitration")
    paper_signal_arbitration.add_argument("--as-of-date", required=True)
    paper_signal_arbitration.add_argument("--model-signals", required=True)
    paper_signal_arbitration.add_argument("--llm-proposals", required=True)
    paper_signal_arbitration.add_argument("--readiness", required=True)
    paper_signal_arbitration.add_argument("--features")
    paper_signal_arbitration.add_argument("--shadow-plan")
    paper_signal_arbitration.add_argument("--challenger-signals")
    paper_signal_arbitration.add_argument("--output-dir", default="reports/tmp/paper_signal_arbitration")
    paper_signal_arbitration.set_defaults(func=handlers.paper_signal_arbitration)

    challenger_signals = subparsers.add_parser("paper-challenger-signals")
    challenger_signals.add_argument("--as-of-date", required=True)
    challenger_signals.add_argument("--model-run", required=True)
    challenger_signals.add_argument("--features", required=True)
    challenger_signals.add_argument("--readiness", required=True)
    challenger_signals.add_argument("--output-dir", default="reports/tmp/paper_challenger_signals")
    challenger_signals.set_defaults(func=handlers.paper_challenger_signals)

    shadow_outcome = subparsers.add_parser("paper-shadow-outcome-report")
    shadow_outcome.add_argument("--as-of-date", required=True)
    shadow_outcome.add_argument("--signal-plan", required=True)
    shadow_outcome.add_argument("--approved-dir", required=True)
    shadow_outcome.add_argument("--ledger-output", required=True)
    shadow_outcome.add_argument("--horizon-days", type=int, default=1)
    shadow_outcome.add_argument("--output-dir", default="reports/tmp/paper_shadow")
    shadow_outcome.set_defaults(func=handlers.paper_shadow_outcome_report)

    shadow_scorecard = subparsers.add_parser("paper-shadow-scorecard")
    shadow_scorecard.add_argument("--ledger-input", required=True)
    shadow_scorecard.add_argument("--phase-review", required=True)
    shadow_scorecard.add_argument("--paper-performance", required=True)
    shadow_scorecard.add_argument("--min-shadow-trades", type=int, default=20)
    shadow_scorecard.add_argument("--min-win-rate", type=float, default=0.50)
    shadow_scorecard.add_argument("--min-avg-forward-return-bps", type=float, default=0.0)
    shadow_scorecard.add_argument("--max-shadow-drawdown-pct", type=float, default=10.0)
    shadow_scorecard.add_argument("--max-missing-outcome-rate-pct", type=float, default=5.0)
    shadow_scorecard.add_argument("--output-dir", default="reports/tmp/paper_shadow_scorecard")
    shadow_scorecard.set_defaults(func=handlers.paper_shadow_scorecard)

    alias = subparsers.add_parser("paper-model-alias-decision")
    alias.add_argument("--shadow-scorecard", required=True)
    alias.add_argument("--review-decision", required=True)
    alias.add_argument("--candidate-model-run", required=True)
    alias.add_argument("--latest-model", required=True)
    alias.add_argument("--reviewer", required=True)
    alias.add_argument("--reason", required=True)
    alias.add_argument("--ttl-days", type=int, default=30)
    alias.add_argument("--output-dir", default="reports/tmp/paper_model_alias")
    alias.set_defaults(func=handlers.paper_model_alias_decision)

    paper_shadow = subparsers.add_parser("paper-challenger-shadow-plan")
    paper_shadow.add_argument("--challenger-report", required=True)
    paper_shadow.add_argument("--review-decision", required=True)
    paper_shadow.add_argument("--latest-model", required=True)
    paper_shadow.add_argument("--approved-manifest", required=True)
    paper_shadow.add_argument("--feature-schema", required=True)
    paper_shadow.add_argument("--output-dir", default="reports/tmp/paper_challenger_shadow")
    paper_shadow.set_defaults(func=handlers.paper_challenger_shadow_plan)

    paper_autopilot = subparsers.add_parser("paper-autopilot-plan")
    paper_autopilot.add_argument("--as-of-date", required=True)
    paper_autopilot.add_argument("--readiness", required=True)
    paper_autopilot.add_argument("--ops-check")
    paper_autopilot.add_argument("--evidence-index")
    paper_autopilot.add_argument("--llm-review")
    paper_autopilot.add_argument("--human-review")
    paper_autopilot.add_argument("--permissions", default="configs/permissions.yml")
    paper_autopilot.add_argument("--output-dir", default="reports/tmp/paper_autopilot_plan")
    paper_autopilot.set_defaults(func=handlers.paper_autopilot_plan)

    paper_review = subparsers.add_parser("paper-review-decision")
    paper_review.add_argument("--as-of-date", required=True)
    paper_review.add_argument("--decision", required=True)
    paper_review.add_argument("--reviewer", required=True)
    paper_review.add_argument("--reason", required=True)
    paper_review.add_argument("--output-dir", default="reports/tmp/paper_reviews")
    paper_review.set_defaults(func=handlers.paper_review_decision)

    paper_bot_cycle = subparsers.add_parser("paper-bot-cycle")
    paper_bot_cycle.add_argument("--as-of-date", required=True)
    paper_bot_cycle.add_argument("--readiness", required=True)
    paper_bot_cycle.add_argument("--human-review", required=True)
    paper_bot_cycle.add_argument("--llm-review")
    paper_bot_cycle.add_argument("--ops-check")
    paper_bot_cycle.add_argument("--evidence-index")
    paper_bot_cycle.add_argument("--signal-plan")
    paper_bot_cycle.add_argument("--permissions", default="configs/permissions.yml")
    paper_bot_cycle.add_argument("--output-dir", default="reports/tmp/paper_bot_cycle")
    paper_bot_cycle.add_argument("--confirm-readiness", action="store_true")
    paper_bot_cycle.add_argument("--confirm-paper", action="store_true")
    paper_bot_cycle.add_argument("--confirm-auto-submit", action="store_true")
    paper_bot_cycle.add_argument("--confirm-auto-close", action="store_true")
    paper_bot_cycle.add_argument("--require-clean-state", action="store_true")
    paper_bot_cycle.set_defaults(func=handlers.paper_bot_cycle)

    paper_auto_cycle = subparsers.add_parser("paper-auto-cycle")
    paper_auto_cycle.add_argument("--as-of-date", required=True)
    auto_source = paper_auto_cycle.add_mutually_exclusive_group(required=True)
    auto_source.add_argument("--source")
    auto_source.add_argument("--approved-dir")
    paper_auto_cycle.add_argument("--dataset-id", default="core_etfs")
    paper_auto_cycle.add_argument("--frequency", default="1d", choices=("1d", "1h"))
    paper_auto_cycle.add_argument("--from", dest="start", required=True)
    paper_auto_cycle.add_argument("--to", dest="end", required=True)
    paper_auto_cycle.add_argument("--provider", default="manual_csv")
    paper_auto_cycle.add_argument("--license-note")
    paper_auto_cycle.add_argument("--config", default="configs/universe.yml")
    paper_auto_cycle.add_argument("--risk", default="configs/risk.yml")
    paper_auto_cycle.add_argument("--signal-model", default="models/latest_model.json")
    paper_auto_cycle.add_argument("--paper-model-alias")
    paper_auto_cycle.add_argument("--approved-output-dir", default="data/raw/approved")
    paper_auto_cycle.add_argument("--registry-dir", default="reports/registry")
    paper_auto_cycle.add_argument("--output-dir", default="reports/tmp/paper_auto_cycle")
    paper_auto_cycle.add_argument("--monitor")
    paper_auto_cycle.add_argument("--performance")
    paper_auto_cycle.add_argument("--operator-status")
    paper_auto_cycle.add_argument("--campaign-report")
    paper_auto_cycle.add_argument("--lock-dir")
    paper_auto_cycle.add_argument("--session-ledger")
    paper_auto_cycle.add_argument("--require-clean-state", action="store_true")
    paper_auto_cycle.add_argument("--confirm-paper-auto", action="store_true")
    paper_auto_cycle.add_argument("--use-openai", action="store_true")
    paper_auto_cycle.add_argument("--confirm-llm", action="store_true")
    paper_auto_cycle.set_defaults(func=handlers.paper_auto_cycle)

    paper_daily = subparsers.add_parser("paper-daily")
    paper_daily.add_argument("--config", default=paper_daily_default_config)
    paper_daily.add_argument("--source-csv")
    paper_daily.add_argument("--from", dest="start")
    paper_daily.add_argument("--to", dest="end")
    paper_daily.add_argument("--as-of-date")
    paper_daily.add_argument("--session-dir")
    paper_daily.add_argument("--sessions-root")
    paper_daily.add_argument("--ledger-output")
    paper_daily.add_argument("--output")
    paper_daily.add_argument("--markdown-output")
    paper_daily.add_argument("--confirm-paper", action="store_true")
    paper_daily.add_argument("--confirm-auto-close", action="store_true")
    paper_daily.add_argument("--confirm-auto-submit", action="store_true")
    paper_daily.add_argument("--send-telegram", action="store_true")
    paper_daily.add_argument("--telegram-dry-run", action="store_true")
    paper_daily.add_argument("--telegram-send-warnings", action="store_true")
    paper_daily.set_defaults(func=handlers.paper_daily)

    paper_daily_from_readiness = subparsers.add_parser("paper-daily-from-readiness")
    paper_daily_from_readiness.add_argument("--readiness", required=True)
    paper_daily_from_readiness.add_argument("--output-dir")
    paper_daily_from_readiness.add_argument("--ledger-output")
    paper_daily_from_readiness.add_argument("--confirm-readiness", action="store_true")
    paper_daily_from_readiness.add_argument("--confirm-paper", action="store_true")
    paper_daily_from_readiness.add_argument("--confirm-auto-close", action="store_true")
    paper_daily_from_readiness.add_argument("--confirm-auto-submit", action="store_true")
    paper_daily_from_readiness.add_argument("--require-clean-state", action="store_true")
    paper_daily_from_readiness.set_defaults(func=handlers.paper_daily_from_readiness)

    prepare_paper_daily_parser = subparsers.add_parser("prepare-paper-daily")
    prepare_source = prepare_paper_daily_parser.add_mutually_exclusive_group(required=True)
    prepare_source.add_argument("--source")
    prepare_source.add_argument("--approved-dir")
    prepare_paper_daily_parser.add_argument("--dataset-id")
    prepare_paper_daily_parser.add_argument("--frequency", choices=("1d", "1h"))
    prepare_paper_daily_parser.add_argument("--from", dest="start", required=True)
    prepare_paper_daily_parser.add_argument("--to", dest="end", required=True)
    prepare_paper_daily_parser.add_argument("--as-of-date", required=True)
    prepare_paper_daily_parser.add_argument("--provider", default="manual_csv")
    prepare_paper_daily_parser.add_argument("--license-note")
    prepare_paper_daily_parser.add_argument("--config", default="configs/universe.yml")
    prepare_paper_daily_parser.add_argument("--risk", default="configs/risk.yml")
    prepare_paper_daily_parser.add_argument("--signal-model", default="models/latest_model.json")
    prepare_paper_daily_parser.add_argument("--paper-model-alias")
    prepare_paper_daily_parser.add_argument("--reference-features")
    prepare_paper_daily_parser.add_argument("--candidate-spec")
    prepare_paper_daily_parser.add_argument("--approved-output-dir", default="data/raw/approved")
    prepare_paper_daily_parser.add_argument("--output-dir", default="reports/tmp/paper_daily_prepare")
    prepare_paper_daily_parser.add_argument("--registry-dir", default="reports/registry")
    prepare_paper_daily_parser.add_argument("--periods-per-year", default="auto")
    prepare_paper_daily_parser.add_argument("--min-accuracy-lift", type=float, default=0.02)
    prepare_paper_daily_parser.add_argument("--min-test-samples", type=int, default=30)
    prepare_paper_daily_parser.add_argument("--run-offline-smoke", action="store_true")
    prepare_paper_daily_parser.set_defaults(func=handlers.prepare_paper_daily)
