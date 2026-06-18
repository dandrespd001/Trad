"""Paper-trading CLI parser wiring."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Callable


CliHandler = Callable[[argparse.Namespace], int]


@dataclass(frozen=True)
class PaperCliHandlers:
    paper: CliHandler
    paper_audit: CliHandler
    paper_session: CliHandler
    paper_execute_session: CliHandler
    paper_close_session: CliHandler
    paper_observability: CliHandler
    paper_monitor: CliHandler
    paper_campaign_report: CliHandler
    paper_day_close: CliHandler
    paper_performance_report: CliHandler
    paper_statement_validate: CliHandler
    paper_weekly_summary: CliHandler
    paper_ops_check: CliHandler
    paper_ops_rehearsal: CliHandler
    paper_evidence_index: CliHandler
    paper_daily: CliHandler
    paper_daily_from_readiness: CliHandler
    prepare_paper_daily: CliHandler


def add_paper_subcommands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    handlers: PaperCliHandlers,
    paper_daily_default_config: str,
) -> None:
    paper = subparsers.add_parser("paper")
    paper.add_argument("--broker", default="alpaca")
    paper.add_argument("--dry-run", action="store_true", default=True)
    paper.add_argument("--real-paper", action="store_true")
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
    paper_execute.add_argument("--output-dir")
    paper_execute.add_argument("--as-of-date", default="today")
    paper_execute.add_argument("--max-feature-age-days", type=int, default=5)
    paper_execute.add_argument("--ledger-output")
    paper_execute.set_defaults(func=handlers.paper_execute_session)

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
    paper_campaign.add_argument("--ledger-input", action="append", default=[])
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
        choices=("complete", "missing-performance", "stop", "invalid-statement"),
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
    prepare_paper_daily_parser.add_argument("--reference-features")
    prepare_paper_daily_parser.add_argument("--approved-output-dir", default="data/raw/approved")
    prepare_paper_daily_parser.add_argument("--output-dir", default="reports/tmp/paper_daily_prepare")
    prepare_paper_daily_parser.add_argument("--registry-dir", default="reports/registry")
    prepare_paper_daily_parser.add_argument("--periods-per-year", default="auto")
    prepare_paper_daily_parser.add_argument("--min-accuracy-lift", type=float, default=0.02)
    prepare_paper_daily_parser.add_argument("--min-test-samples", type=int, default=30)
    prepare_paper_daily_parser.add_argument("--run-offline-smoke", action="store_true")
    prepare_paper_daily_parser.set_defaults(func=handlers.prepare_paper_daily)
