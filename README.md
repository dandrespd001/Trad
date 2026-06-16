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
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --signal-model models/latest_model.json --features data/processed/features.csv --submit-signal-order --output /tmp/trading_ai_signal_order_dry_run.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --list-orders --output /tmp/trading_ai_paper_orders.json
```

`ingest` currently supports deterministic local sample data and `--source-csv`
for approved external downloads prepared outside the bot. Writing Parquet is
available through the IO layer when `pandas` and `pyarrow` are installed.
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
