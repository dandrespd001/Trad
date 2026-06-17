# Trading AI Research Scaffold

This workspace implements a conservative MVP for the trading AI plan in
`docs/trading-bot-ai-research.md`: config validation, OHLCV data validation,
feature engineering, deterministic momentum/volatility-target backtesting,
Markdown reports, OpenAI Responses API request scaffolding, and Alpaca paper
dry-run boundaries.

Current scope:

- no live-money trading;
- no live broker credentials;
- no live-money order execution;
- no model-driven trade authority;
- Alpaca paper adapter defaults to dry-run;
- real Alpaca paper access requires `--real-paper`, `--confirm-paper`, process
  environment credentials, and the optional `broker` dependency;
- model-driven Alpaca paper orders are limited to an explicit signal-to-order
  CLI flow with a fixed USD 1 notional, ETF allowlist, risk gates, and JSON
  audit output;
- pure-Python core paths for tests and local smoke checks.

The package is pinned to Python 3.12 in `pyproject.toml`. The current shell may
still run source-path tests with another interpreter, but install/build should
use Python 3.12.

Recommended local checks from the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m json.tool notebooks/benchmark_baseline_vs_ml_vs_timeseries.ipynb >/dev/null
```

CLI smoke flow without network or heavy research packages:

```bash
PYTHONPATH=src python3 -m trading_ai.cli ingest --config configs/universe.yml --from 2024-01-01 --to 2024-03-31 --output /tmp/trading_ai_etfs.csv
PYTHONPATH=src python3 -m trading_ai.cli validate-data --dataset /tmp/trading_ai_etfs.csv
PYTHONPATH=src python3 -m trading_ai.cli manifest --dataset /tmp/trading_ai_etfs.csv --output /tmp/trading_ai_etfs.manifest.json
PYTHONPATH=src python3 -m trading_ai.cli build-features --dataset /tmp/trading_ai_etfs.csv --output /tmp/trading_ai_features.csv
PYTHONPATH=src python3 -m trading_ai.cli backtest --strategy momentum-vol-target --config configs/risk.yml --dataset /tmp/trading_ai_etfs.csv --output /tmp/trading_ai_backtest.json --report-output /tmp/trading_ai_backtest.md
PYTHONPATH=src python3 -m trading_ai.cli report --run-id /tmp/trading_ai_backtest.json --output /tmp/trading_ai_report.md
PYTHONPATH=src python3 -m trading_ai.cli train --model logistic-baseline --dataset /tmp/trading_ai_features.csv --output /tmp/trading_ai_model.json --run-output /tmp/trading_ai_model_run.json
PYTHONPATH=src python3 -m trading_ai.cli evaluate --run-id /tmp/trading_ai_model_run.json --output /tmp/trading_ai_model_eval.json
PYTHONPATH=src python3 -m trading_ai.cli promote --run-id /tmp/trading_ai_model_run.json --baseline /tmp/baseline_classifier_metrics.json --output /tmp/trading_ai_promotion.json
PYTHONPATH=src python3 -m trading_ai.cli llm-eval --output /tmp/trading_ai_llm_guardrail_eval.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --universe configs/universe.yml --risk configs/risk.yml
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --universe configs/universe.yml --risk configs/risk.yml --kill-switch-test --output /tmp/trading_ai_paper_kill_switch.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --read-account --output /tmp/trading_ai_paper_status.json
PYTHONPATH=src python3 -m trading_ai.cli ingest --config configs/universe.yml --from 2026-03-01 --to 2026-06-16 --output /tmp/trading_ai_fresh_source.csv
PYTHONPATH=src python3 -m trading_ai.cli paper-session --source-csv /tmp/trading_ai_fresh_source.csv --from 2026-03-01 --to 2026-06-16 --as-of-date 2026-06-16 --output-dir /tmp/trading_ai_paper_session
# Optional paper-only broker execution after manual review; not part of offline smoke.
# PYTHONPATH=src python3 -m trading_ai.cli paper-execute-session --session-dir /tmp/trading_ai_paper_session --confirm-paper --confirm-submit
# Optional paper-only closeout after the broker reports fill/position evidence.
# PYTHONPATH=src python3 -m trading_ai.cli paper-close-session --session-dir /tmp/trading_ai_paper_session --confirm-paper
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --list-orders --output /tmp/trading_ai_paper_orders.json
PYTHONPATH=src python3 -m trading_ai.cli paper-observability --sessions-root /tmp --session-dir /tmp/trading_ai_paper_session --output /tmp/trading_ai_paper_observability.json --markdown-output /tmp/trading_ai_paper_observability.md
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor --sessions-root /tmp --session-dir /tmp/trading_ai_paper_session --output /tmp/trading_ai_paper_monitor.json --markdown-output /tmp/trading_ai_paper_monitor.md --as-of-date 2026-06-16
```

`ingest` currently supports deterministic local sample data and `--source-csv`
for approved external downloads prepared outside the bot. Governed real-data
imports use `import-approved-data`, which validates a manually downloaded CSV
from an approved provider, writes canonical Parquet to
`data/raw/approved/<dataset_id>/<frequency>/ohlcv.parquet`, and emits
`manifest.json` plus `catalog_entry.json`. Parquet requires the research extras:
`pip install -e ".[research]"`. Providers are declared in
`configs/data_sources.yml`; `manual_csv` is enabled without network access, and
`api_placeholder` is intentionally disabled with `api_provider_not_enabled`.
`refresh-data` reads an approved local CSV or Parquet path through
`--source-csv` or the `--source` alias, filters it to the configured ETF universe
and date range, validates OHLCV quality, builds features, checks freshness on
the latest model-usable feature rows, and writes `raw.csv`, `features.csv`,
`raw_manifest.json`, `features_manifest.json`, and `freshness.json` under
`reports/tmp/fresh_data` by default. It does not download data, retrain models,
contact brokers, or submit orders.
The recommended paper-review sequence is:

```bash
PYTHONPATH=src python3 -m trading_ai.cli import-approved-data --source /path/to/approved.csv --dataset-id core_etfs --frequency 1d --config configs/universe.yml --provider manual_csv --license-note "manual download approved for research use" --output-dir data/raw/approved --as-of-date 2026-06-16
PYTHONPATH=src python3 -m trading_ai.cli evaluate-approved-data --approved-dir data/raw/approved/core_etfs/1d --config configs/universe.yml --risk configs/risk.yml --output-dir reports/tmp/approved_eval --as-of-date 2026-06-16
```

`evaluate-approved-data` is the reproducible offline eligibility gate for
approved Parquet packages. It requires `ohlcv.parquet`, `manifest.json`, and
`catalog_entry.json`; verifies the dataset hash from the manifest; validates
symbols and `1d`/`1h` timestamps against the configured universe; runs the
deterministic momentum/volatility-target backtest and logistic baseline
evaluation; compares against a majority-classifier baseline; and writes
`data_quality.json`, `backtest.json`, `backtest.md`, `model_run.json`,
`model_eval.json`, `promotion_decision.json`, `evaluation_summary.json`, and
`evaluation_summary.md` under
`reports/tmp/approved_eval/<dataset_id>/<frequency>/<as_of_date>/`. Exit code
`0` means `eligible_for_paper_challenger=true`; exit code `1` means the package
was evaluated but blocked or rejected by gates; exit code `2` means an
operational problem such as missing Parquet dependencies, required files,
manifest errors, or hash mismatch. It does not read credentials, contact
brokers, replace `models/latest_model.json`, submit signals, or download data.
Only after `evaluate-approved-data` returns exit code `0` should a
`paper-session` be prepared for paper review.
MLflow remains an optional mirror around the local JSON registry. Use
`sync-registry-mlflow`, `register-registry-mlflow-model`, and
`review-mlflow-paper-candidate` to publish and validate the
`paper-candidate` alias, but that reviewed MLflow model does not automatically
replace `--signal-model` or `models/latest_model.json`.
`paper-session` remains the offline smoke package for paper review. It runs
approved-CSV refresh, optional drift monitoring when `--reference-features` is
provided, dry-run signal-order generation, and `paper-audit` in one reproducible
run. It writes `fresh_data/`, optional `monitoring/`, `paper/`, `audit/`,
`session.json`, and `session.md` under `reports/tmp/paper_session/latest` by
default. It does not read Alpaca credentials, build a real broker client,
download data, or submit live orders. Exit code `0` means
`ready_for_paper_review=true`; exit code `1` means freshness, preflight, or audit
blocked the package; exit code `2` means an operational error such as an invalid
source CSV or missing path.
With `--review-mlflow-paper-candidate`, `paper-session` also runs the offline
MLflow alias review after writing `fresh_data/features.csv` and before
`paper-audit`. It writes `mlflow/paper_candidate_review.json` and
`mlflow/paper_candidate_review.md`; `PASSED` is recorded in `session.json` and
audit summary, while `FAILED` blocks the paper package with exit code `1`.
Operational review errors such as missing MLflow or an unreadable registry
return exit code `2`. If no features were generated, the session writes a local
failed review and exits `1`.
`paper-execute-session` is the controlled bridge from an approved offline
`paper-session` package to one Alpaca paper order. Use the dedicated runbook in
[`docs/paper-real-runbook.md`](docs/paper-real-runbook.md) before running it
against a real paper account. The command requires
`--session-dir <dir> --confirm-paper --confirm-submit`; optional flags are
`--output-dir`, `--as-of-date`, and `--max-feature-age-days`. It performs local
package validation before reading `ALPACA_PAPER_API_KEY` and
`ALPACA_PAPER_SECRET_KEY` from the process environment, not `.env`, then writes
`execution/paper_execution.json` and `execution/paper_execution.md` by default.
It does not authorize live trading, download data, recalculate signals, retrain
models, or modify the approved offline package. Exit code `0` means submitted,
`1` means blocked or rejected, and `2` means an operational CLI/package error.
`paper-close-session` closes the evidence loop after `paper-execute-session`.
It requires `--session-dir <dir> --confirm-paper`; optional flags are
`--execution-report`, `--output-dir`, and `--ledger-output`. The command first
revalidates the local session, audit, signal, freshness, and execution report,
then reads Alpaca paper credentials and queries account, positions, open orders,
and the expected `client_order_id`. It never sends or cancels orders. By
default it writes `closeout/paper_closeout.json` and
`closeout/paper_closeout.md`. Exit code `0` with `status=CLOSED` means the
submitted USD 1 paper order matches broker state, has filled or partially
filled, and the expected position exists. Exit code `1` with `status=PENDING`
means the order matches but fill/position evidence is not complete yet and the
command can be rerun. Exit code `1` with `status=UNMATCHED` means the order is
missing, mismatched, or broker state is canceled, rejected, or expired. Exit
code `2` means an operational error such as bad JSON, missing paths,
credentials, or broker dependency.
`paper-observability` is an offline report builder for paper evidence. It reads
existing `session.json`, `audit/paper_audit.json`,
`paper/paper_signal_order.json`, `fresh_data/freshness.json`, optional
`monitoring/drift.json`, optional `execution/paper_execution.json`, optional
`closeout/paper_closeout.json`, and optional JSONL ledgers, then writes
aggregate JSON/Markdown under
`reports/tmp/paper_observability` by default. It does not contact brokers, read
credentials, download data, submit orders, or authorize live trading. Repeated
`--session-dir` includes specific sessions; repeated `--ledger-input` includes
append-only event ledgers.
`paper-monitor` is the paper-only daily dashboard after `paper-observability`.
It reuses the same offline evidence and writes
`reports/tmp/paper_monitor/latest.json` plus
`reports/tmp/paper_monitor/latest.md` by default. `status=OK` exits `0`;
`status=WARN` exits `0`; `status=CRITICAL` exits `1`; operational failures
exit `2`. Critical alerts include observability diagnostics for missing or
invalid session artifacts, blocked sessions, blocked executions, closeouts in
`PENDING` or `UNMATCHED`, submitted executions without closeout evidence, and
existing observability blockers. Warnings include missing requested ledgers, no
recent sessions, or incomplete non-critical evidence such as a ready session
without execution evidence. The monitor does not contact Alpaca, read broker
credentials, download data, retrain models, recalculate signals, submit orders,
cancel orders, or authorize live trading.
Telegram notifications are disabled by default. Use `--telegram-dry-run` to
write a redacted preview without reading environment variables or making a
network call. Use `--send-telegram` to send one plain-text summary through the
Telegram Bot API `sendMessage`; the command reads only
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the current process
environment and never reads `.env`. By default it sends only critical alerts;
add `--telegram-send-warnings` to include warnings. Artifacts are written before
any Telegram attempt, and send failures exit `2` with redacted error details.
`--ledger-output <path>` is available as an opt-in paper-only append for
`paper-session`, `paper-execute-session`, `paper-close-session`,
`paper-monitor`, and paper order management commands (`--list-orders`,
`--get-order`, `--reconcile-order`, `--cancel-order`). When omitted there are no
new side effects. Each JSONL line
is a redacted event with schema/status/reason fields and order intent
identifiers such as `client_order_id`, `symbol`, `side`, and `notional`; it does
not store account payloads, credentials, or full broker responses. Recommended
paths are under `reports/tmp/paper_observability/`, for example:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-session --source-csv /tmp/trading_ai_fresh_source.csv --from 2026-03-01 --to 2026-06-16 --as-of-date 2026-06-16 --output-dir reports/tmp/paper_session/latest --ledger-output reports/tmp/paper_observability/ledger.jsonl
PYTHONPATH=src python3 -m trading_ai.cli paper-close-session --session-dir reports/tmp/paper_session/latest --confirm-paper --ledger-output reports/tmp/paper_observability/ledger.jsonl
PYTHONPATH=src python3 -m trading_ai.cli paper-observability --sessions-root reports/tmp/paper_session --ledger-input reports/tmp/paper_observability/ledger.jsonl --output reports/tmp/paper_observability/latest.json --markdown-output reports/tmp/paper_observability/latest.md
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor --sessions-root reports/tmp/paper_session --ledger-input reports/tmp/paper_observability/ledger.jsonl --output reports/tmp/paper_monitor/latest.json --markdown-output reports/tmp/paper_monitor/latest.md --ledger-output reports/tmp/paper_observability/ledger.jsonl
```
`train --model logistic-baseline` is a pure-Python Phase 3 baseline for temporal
evaluation plumbing only; it is not a champion model and does not authorize
trading.
`promote` only writes a champion/challenger eligibility decision. Approval means
eligible for paper challenger review, not live trading.
`llm-eval` runs local guardrail evals without network access. The OpenAI
Responses wrapper supports JSONL usage/error logging when called with
`usage_log_path`, but no real API call is required for the local test suite.
`paper --kill-switch-test` proves that new paper orders are rejected while the
kill-switch is active and that cancellation remains available in dry-run mode.
`paper --submit-signal-order` converts the local baseline model's latest valid
feature rows into long/cash signals, selects the highest-probability `buy`
signal, and submits at most one USD 1 notional Alpaca paper order after risk
gates. Use `--dry-run` first. Real paper account reads or signal-order
submission require `--real-paper --confirm-paper`, a Python 3.12 environment
with `python -m pip install -e ".[broker]"`, and process environment variables
`ALPACA_PAPER_API_KEY` and `ALPACA_PAPER_SECRET_KEY`.
`paper-audit` reads existing `refresh-data`, signal-order, and optional
reconciliation/backtest/promotion/drift JSON reports, then writes an offline
audit journal under `reports/tmp/paper_audit` by default. The optional
`--mlflow-candidate-review-report <path>` is blocking only when provided:
`status=PASSED` adds MLflow summary fields, while a missing, invalid, or
non-passing report produces `mlflow_candidate_review_failed`. It does not
download data, read credentials, contact brokers, or submit orders. Exit code
`0` means no blocking findings; exit code `1` means the session is blocked for
manual paper review.
`paper --list-orders`, `paper --get-order`, `paper --reconcile-order`, and
`paper --cancel-order` manage Alpaca paper order state. Cancellation always
requires `--confirm-cancel`. Typical real-paper order checks:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --real-paper --confirm-paper --list-orders --order-status open --output reports/alpaca_open_orders.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --real-paper --confirm-paper --get-order --client-order-id signal-spy-20240329 --output reports/alpaca_order_signal_spy.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --real-paper --confirm-paper --reconcile-order --source-report reports/alpaca_signal_order_real.json --output reports/alpaca_signal_order_reconciliation.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --real-paper --confirm-paper --cancel-order --client-order-id signal-spy-20240329 --confirm-cancel --output reports/alpaca_cancel_signal_spy.json
```

Install optional research dependencies only after reviewing
`docs/tooling-risk-register.md` and `configs/permissions.yml`.
