# Trading AI Research Scaffold

This workspace implements a conservative MVP for the trading AI plan in
`docs/trading-bot-ai-research.md`: config validation, OHLCV data validation,
feature engineering, deterministic momentum/volatility-target backtesting,
Markdown reports, local-only LLM supervision scaffolding, and Alpaca paper
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
  CLI flow with governed paper notional (`CANARY` USD 1; `SCALE_UP`/`READINESS`
  up to USD 5 only with reviewer/reason and clean evidence), ETF allowlist,
  risk gates, and JSON audit output;
- `paper-auto-cycle` is the only cronable paper-auto wrapper; it remains
  paper-only, uses governed LLM signal proposals with `llm_authority=none`,
  writes compact daily evidence, and requires `--confirm-paper-auto` before it
  can call the paper-confirmed path;
- pure-Python core paths for tests and local smoke checks.

The package is pinned to Python 3.12 in `pyproject.toml`. The current shell may
still run source-path tests with another interpreter, but install/build should
use Python 3.12.

Recommended local checks from the repository root:

```bash
./scripts/verify-paper-environment.sh
./scripts/verify-release-minimal.sh
./scripts/verify-release.sh
./scripts/verify-paper-focused.sh
./scripts/verify-paper-artifacts.sh
./scripts/verify-paper-gates.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -q
python3 -m json.tool notebooks/benchmark_baseline_vs_ml_vs_timeseries.ipynb >/dev/null
```

The official verification gate remains stdlib `unittest`. The minimal local
release gate `scripts/verify-release-minimal.sh` runs a core environment check
without optional research or broker dependencies, focused paper tests, a scoped
stdlib `unittest` suite, `git diff --check`, `models/latest_model.json`
immutability, and the live/futures safety scans. It is the no-network,
no-secrets gate for base environments where pytest-style tests or dev extras may
not be installed. The versioned paper wrapper `scripts/verify-paper-gates.sh`
runs focused paper tests, the full `unittest` suite, `git diff --check`, and
`scripts/verify-paper-artifacts.sh`. The broader release gate
`scripts/verify-release.sh` adds environment, quality, dependency, static
security, live-authorization, futures-execution parser checks, and the optional
dev coverage profile for operator-ready changes. Its default `pip-audit` step
is a local dry-run because a live vulnerability query discloses package
inventory to an external service; run a real network audit only from an
approved environment by setting `VERIFY_RELEASE_PIP_AUDIT_CMD=pip-audit-network`.
Run `scripts/verify-paper-environment.sh` before daily paper readiness to fail
fast on the Python 3.12 and optional research dependency setup required for
approved-data Parquet artifacts. Add `--require-broker` before Alpaca
paper-confirmed runs.
For the shortest safe operator path, use `docs/paper-quickstart.md` and the
safe wrapper `scripts/run-paper-daily-safe.sh`.
The artifact gate checks for generated report outputs outside `reports/tmp`
and confirms paper monitor/campaign artifacts keep
`live_trading_authorized=false`.

GitHub Actions mirrors the same paper gate in
`.github/workflows/paper-gates.yml` for pushes to `master`/`codex/**` and pull
requests. The workflow installs the package with Python 3.12, does not use
secrets, and repeats safety scans for unchanged `models/latest_model.json`, no
live-trading authorization strings, and no futures execute/submit parsers.

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
# Optional daily operator after configuring an approved fresh source/date.
# PYTHONPATH=src python3 -m trading_ai.cli paper-daily --config configs/paper_daily.yml
# Optional paper-only broker execution after manual review; not part of offline smoke.
# PYTHONPATH=src python3 -m trading_ai.cli paper-execute-session --session-dir /tmp/trading_ai_paper_session --confirm-paper --confirm-submit
# Optional paper-only closeout after the broker reports fill/position evidence.
# PYTHONPATH=src python3 -m trading_ai.cli paper-close-session --session-dir /tmp/trading_ai_paper_session --confirm-paper
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --list-orders --output /tmp/trading_ai_paper_orders.json
PYTHONPATH=src python3 -m trading_ai.cli paper-observability --sessions-root /tmp --session-dir /tmp/trading_ai_paper_session --output /tmp/trading_ai_paper_observability.json --markdown-output /tmp/trading_ai_paper_observability.md
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor --sessions-root /tmp --session-dir /tmp/trading_ai_paper_session --output /tmp/trading_ai_paper_monitor.json --markdown-output /tmp/trading_ai_paper_monitor.md --as-of-date 2026-06-16
```

Generated CLI outputs default to `reports/tmp/<command>/latest.*` or a
command-specific subdirectory under `reports/tmp`. Historical smoke snapshots
that should remain versioned live under `reports/historical/`; reusable fixtures
such as `data/raw/etfs.csv`, `data/raw/etfs.manifest.json`,
`data/processed/features.csv`, `models/latest_model.json`, and the benchmark
notebook remain versioned fixtures. Pass explicit `--output`,
`--markdown-output`, or `--output-dir` only when you intentionally want a
different destination.

`ingest` currently supports deterministic local sample data and `--source-csv`
for approved external downloads prepared outside the bot. Governed real-data
imports use `import-approved-data`, which validates a manually downloaded CSV
from an approved provider, writes canonical Parquet to
`data/raw/approved/<dataset_id>/<frequency>/ohlcv.parquet`, and emits
`manifest.json` plus `catalog_entry.json`. Parquet requires the research extras:
`pip install -e ".[research]"`. Providers are declared in
`configs/data_sources.yml`; `manual_csv` is enabled without network access, and
`api_placeholder` is intentionally disabled with `api_provider_not_enabled`.
Manual source files under `data/raw/manual/` and approved packages under
`data/raw/approved/` are local artifacts and must not be committed. The current
real-data seed used for paper readiness is `core_etfs/1d`, `2024-01-02` through
`2026-06-18`, with dataset hash
`9a882722e6f5358d69c8c19a8e59bf2845259c3828e80f86e00ddc767d11e7ad` recorded in
`data/raw/approved/core_etfs/1d/manifest.json`.
`refresh-data` reads an approved local CSV or Parquet path through
`--source-csv` or the `--source` alias, filters it to the configured ETF universe
and date range, validates OHLCV quality, builds features, checks freshness on
the latest model-usable feature rows, and writes `raw.csv`, `features.csv`,
`raw_manifest.json`, `features_manifest.json`, and `freshness.json` under
`reports/tmp/fresh_data` by default. It does not download data, retrain models,
contact brokers, or submit orders.
The recommended paper-review sequence is:

```bash
PYTHONPATH=src python3 -m trading_ai.cli prepare-paper-daily --source /path/to/approved.csv --dataset-id core_etfs --frequency 1d --from 2026-03-01 --to 2026-06-16 --as-of-date 2026-06-16 --config configs/universe.yml --risk configs/risk.yml --signal-model models/latest_model.json --license-note "manual download approved for research use" --run-offline-smoke

# Or reuse an existing approved package:
PYTHONPATH=src python3 -m trading_ai.cli prepare-paper-daily --approved-dir data/raw/approved/core_etfs/1d --from 2026-03-01 --to 2026-06-16 --as-of-date 2026-06-16 --config configs/universe.yml --risk configs/risk.yml --signal-model models/latest_model.json --run-offline-smoke
```

`prepare-paper-daily` is the preferred daily readiness gate. It imports or
reuses an approved package, evaluates it, registers the approved evaluation,
writes `readiness.json`/`readiness.md`, generates
`paper_daily.generated.yml`, and, with `--run-offline-smoke`, executes
`paper-daily` offline from that generated config. The smoke uses no broker
confirmations and `send_telegram=false`; it does not read `.env`, broker
credentials, or Telegram credentials, does not build an Alpaca client, does not
download data, and does not mutate `models/latest_model.json`. Review
`offline_smoke.requested`, `offline_smoke.ran`, `offline_smoke.exit_code`, and
the daily/session/observability/monitor artifact paths before any
broker-confirmed run. Exit code `0` means readiness and the offline smoke
passed; `1` means the evaluation or smoke blocked paper daily; `2` means an
operational error. Broker-inclusive paper runs must go through
`paper-daily-from-readiness`, which revalidates the readiness report and writes
fresh broker-confirmed evidence under
`paper_daily/broker_confirmed/` without overwriting the offline smoke artifacts.

The cronable paper-auto flow wraps the same readiness package and adds governed
LLM proposals plus deterministic arbitration:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-auto-cycle \
  --source /path/to/approved.csv \
  --dataset-id core_etfs \
  --frequency 1d \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16 \
  --license-note "manual download approved for paper use" \
  --output-dir reports/tmp/paper_auto_cycle \
  --lock-dir reports/tmp/paper_auto_cycle/locks
```

Without `--confirm-paper-auto`, the wrapper stops after evidence: import or
reuse approved data, `prepare-paper-daily --run-offline-smoke`, local
`llm_context_digest`, `llm-signal-proposals`, `paper-signal-arbitration`,
`ops_check`, `evidence_index`, `cycle.json|md`, and `daily_status.json|md`.
The auto cycle fails fast if prepare did not produce a real session signal
report and features artifact; it does not synthesize empty features or signals.
With `--confirm-paper-auto`, it may create an automatic paper review and call
`paper-bot-cycle`, which still requires the existing paper-only confirmations
plus `--require-clean-state`. Confirmed auto cycles reject relative date values
such as `today`; pass explicit ISO dates for `--as-of-date`, `--from`, and
`--to` so the broker evidence can be replayed exactly.
It never reads `.env`; broker credentials are read only by the lower-level
paper adapter after the confirmed paper stage is reached.

`paper-auto-cycle` accepts optional local operational evidence:
`--monitor <paper-monitor.json>` and `--performance <paper-performance.json>`.
Those files are read-only kill-switch inputs. `CRITICAL`/`ERROR` monitor state,
open broker orders, existing paper positions, pending or unmatched closeouts,
statement mismatches, unreconciled fills, or safety fields that report
credentials/live/order side effects block the cycle before review or broker
calls. For cron, prefer:

```bash
scripts/run-paper-auto-cycle.sh \
  --source /path/to/approved.csv \
  --dataset-id core_etfs \
  --frequency 1d \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16 \
  --license-note "manual download approved for paper use"
```

For confirmed paper automation, generate a clean operator status first and pass
it back into the cycle:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-operator-status \
  --as-of-date 2026-06-16 \
  --ledger reports/tmp/paper_auto_cycle/session_ledger.jsonl \
  --monitor reports/tmp/paper_monitor/latest.json \
  --performance reports/tmp/paper_performance/latest.json \
  --lock-dir reports/tmp/paper_auto_cycle/locks \
  --max-lock-age-minutes 90 \
  --output-dir reports/tmp/paper_operator_status

PYTHONPATH=src python3 -m trading_ai.cli paper-auto-cycle \
  --approved-dir data/raw/approved/core_etfs/1d \
  --dataset-id core_etfs \
  --frequency 1d \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16 \
  --monitor reports/tmp/paper_monitor/latest.json \
  --performance reports/tmp/paper_performance/latest.json \
  --operator-status reports/tmp/paper_operator_status/2026-06-16/operator_status.json \
  --session-ledger reports/tmp/paper_auto_cycle/session_ledger.jsonl \
  --require-clean-state \
  --confirm-paper-auto
```

Every cycle appends a `PaperSessionRecord` to
`reports/tmp/paper_auto_cycle/session_ledger.jsonl` by default, including
blocked cycles. `paper-campaign-report` and `paper-performance-report` can read
that ledger through `--ledger-input`. `paper-campaign-report` keeps the
20-session `paper_auto_campaign` goal and adds a separate 60-session
`stability_campaign` for manual phase review. `paper-auto-cycle
--require-clean-state` blocks a same-date duplicate if existing cycle or ledger
evidence already shows `PAPER_SUBMITTED` or `PAPER_CLOSED`.

After daily evidence and weekly summary are current, run the review-only phase
gate:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-phase-review-report \
  --as-of-date 2026-06-16 \
  --campaign-report reports/tmp/paper_campaign/latest.json \
  --performance-report reports/tmp/paper_performance/latest.json \
  --operator-status reports/tmp/paper_operator_status/2026-06-16/operator_status.json \
  --strategy-quality reports/tmp/paper_strategy_quality/2026-06-16/strategy_quality.json \
  --evidence-index reports/tmp/paper_evidence_index/2026-06-16/evidence_index.json \
  --weekly-summary reports/tmp/paper_weekly_summary/2026-W25/weekly_summary.json
```

`READY_FOR_REVIEW` means 60 stable broker-confirmed sessions, 20 clean
paper-auto sessions, clean operator/performance/evidence, and strategy quality
`PASS` or `WARN`; it still writes `review_only=true` and
`live_trading_authorized=false`. `paper-strategy-quality` summarizes
baseline/arbitration/challenger/performance and ledger trends without promoting
models or changing risk. `llm-context-pack` builds a local read-only context
bundle from cycle, operator status, quality, phase review, evidence index, and
weekly summary artifacts; it records path hashes and blocks dangerous local
instructions such as live orders, risk changes, broker access, 60-session
bypass, or secret reads.

After `phase_status=READY_FOR_REVIEW`, the controlled adaptive training
sequence is offline and review-only:

```bash
PYTHONPATH=src python3 -m trading_ai.cli adaptive-training-cycle \
  --as-of-date 2026-06-16 \
  --approved-dir data/raw/approved/core_etfs/1d \
  --phase-review reports/tmp/paper_phase_review/2026-06-16/phase_review.json \
  --paper-performance reports/tmp/paper_performance/latest.json \
  --registry-dir reports/registry

PYTHONPATH=src python3 -m trading_ai.cli model-challenger-report \
  --evaluation-dir reports/tmp/approved_eval/core_etfs/1d/2026-06-16 \
  --paper-performance reports/tmp/paper_performance/latest.json \
  --phase-review reports/tmp/paper_phase_review/2026-06-16/phase_review.json \
  --training-cycle reports/tmp/adaptive_training/2026-06-16/training_cycle.json

PYTHONPATH=src python3 -m trading_ai.cli paper-challenger-shadow-plan \
  --challenger-report reports/tmp/model_challenger/challenger_report.json \
  --review-decision reports/tmp/model_challenger_decisions/review_decision.json \
  --latest-model models/latest_model.json \
  --approved-manifest data/raw/approved/core_etfs/1d/manifest.json \
  --feature-schema reports/tmp/approved_eval/core_etfs/1d/2026-06-16/model_run.json

PYTHONPATH=src python3 -m trading_ai.cli paper-challenger-signals \
  --as-of-date 2026-06-16 \
  --model-run reports/tmp/approved_eval/core_etfs/1d/2026-06-16/model_run.json \
  --features data/processed/features.csv \
  --readiness reports/tmp/paper_daily_prepare/core_etfs/1d/2026-06-16/readiness.json \
  --output-dir reports/tmp/paper_challenger_signals

PYTHONPATH=src python3 -m trading_ai.cli paper-shadow-outcome-report \
  --as-of-date 2026-06-16 \
  --signal-plan reports/tmp/paper_signal_arbitration/2026-06-16/signal_plan.json \
  --approved-dir data/raw/approved/core_etfs/1d \
  --ledger-output reports/tmp/paper_shadow/shadow_ledger.jsonl

PYTHONPATH=src python3 -m trading_ai.cli paper-shadow-scorecard \
  --ledger-input reports/tmp/paper_shadow/shadow_ledger.jsonl \
  --phase-review reports/tmp/paper_phase_review/2026-06-16/phase_review.json \
  --paper-performance reports/tmp/paper_performance/latest.json

PYTHONPATH=src python3 -m trading_ai.cli paper-model-alias-decision \
  --shadow-scorecard reports/tmp/paper_shadow_scorecard/shadow_scorecard.json \
  --review-decision reports/tmp/model_challenger_decisions/review_decision.json \
  --candidate-model-run reports/tmp/approved_eval/core_etfs/1d/2026-06-16/model_run.json \
  --latest-model models/latest_model.json \
  --reviewer human \
  --reason "shadow scorecard ready for paper alias"
```

`adaptive-training-cycle` deduplicates by `as_of_date + dataset_hash +
latest_model_hash + cadence` and appends to
`reports/tmp/adaptive_training/cycle_ledger.jsonl`. `--force` can rerun the
offline review package, but records `forced=true` and still writes
`model_mutated=false`. The challenger report blocks on phase not ready,
non-reviewable training cycle, critical drift, critical paper performance, or
incomplete evidence. The shadow plan keeps the current champion fixed and marks
the challenger `shadow_only`; it does not create broker clients or submit
orders. `paper-signal-arbitration --features --shadow-plan --challenger-signals ...` may
record shadow signals, but those records never affect real paper order
eligibility. If the shadow scorecard is ready and a human review decision is
`APPROVE_FOR_NEXT_PAPER_CYCLE`, `paper-model-alias-decision` writes
`reports/tmp/paper_model_alias/current.json` and a standalone paper model
without mutating `models/latest_model.json`. `prepare-paper-daily` and
`paper-auto-cycle` can then use `--paper-model-alias`; invalid or expired
aliases block instead of falling back silently. Verify this path with
`scripts/verify-adaptive-routing.sh`.

LLM role specialization uses a separate local supervised factory. It creates
datasets from audited paper artifacts, exports TRL chat JSONL, verifies cached
open-source model weights, registers LoRA adapters, evaluates candidates, and
activates a local LLM alias only with human approval. External LLM APIs are
disabled at runtime; `--use-openai` is kept only as an explicit blocked path.
Cache verification requires a local registry entry, tokenizer/config files, and
local weight or index files whose total size meets the registry's
`minimum_total_weight_bytes` floor. It reports `weight_total_bytes`, keeps
`local_files_only=True` and `network_allowed=False`, and never downloads model
files. Smoke fixtures are test-only audit fixtures and report `FIXTURE_PASSED`,
not a production model smoke `PASSED`.
Qwen3 local generation is run in no-thinking mode when the tokenizer supports
it (`enable_thinking=False`) so `llm-local-smoke` can enforce strict JSON
without hidden reasoning text. The LLM path remains advisory only:
`llm_authority=none`, no broker access, no risk changes, and no mutation of
`models/latest_model.json`.

Install the local LLM stack separately from the heavier forecasting extras:

```bash
.venv312/bin/python -m pip install -e ".[local-llm]"
```

```bash
PYTHONPATH=src python3 -m trading_ai.cli llm-role-registry

PYTHONPATH=src python3 -m trading_ai.cli llm-training-dataset \
  --role paper_ops_reviewer \
  --as-of-date 2026-06-16 \
  --source-root reports/tmp \
  --output-dir reports/tmp/llm_training

PYTHONPATH=src python3 -m trading_ai.cli llm-supervise-labels \
  --role paper_ops_reviewer \
  --dataset reports/tmp/llm_training/paper_ops_reviewer/2026-06-16/dataset.json \
  --frontier-model deterministic-local-teacher

PYTHONPATH=src python3 -m trading_ai.cli llm-training-export \
  --role paper_ops_reviewer \
  --supervised-dataset reports/tmp/llm_supervision/paper_ops_reviewer/labels.json \
  --format trl-jsonl

PYTHONPATH=src python3 -m trading_ai.cli llm-local-cache-verify \
  --model-id Qwen/Qwen3-0.6B

PYTHONPATH=src python3 -m trading_ai.cli llm-local-sft \
  --role paper_ops_reviewer \
  --base-model-id Qwen/Qwen3-0.6B \
  --training-jsonl reports/tmp/llm_training_export/paper_ops_reviewer/training.jsonl \
  --adapter-dir models/local/adapters/paper_ops_reviewer/qwen3-0.6b-lora

PYTHONPATH=src python3 -m trading_ai.cli llm-eval-suite \
  --role paper_ops_reviewer \
  --candidate reports/tmp/llm_supervision/paper_ops_reviewer/labels.json \
  --holdout reports/tmp/llm_training/paper_ops_reviewer/2026-06-16/holdout.jsonl

PYTHONPATH=src python3 -m trading_ai.cli llm-local-adapter-report \
  --role paper_ops_reviewer \
  --sft-manifest reports/tmp/llm_local_sft/manifest.json \
  --eval-report reports/tmp/llm_eval_suite/paper_ops_reviewer/eval_report.json

PYTHONPATH=src python3 -m trading_ai.cli llm-local-alias-decision \
  --role paper_ops_reviewer \
  --adapter-report reports/tmp/llm_local_adapters/paper_ops_reviewer/adapter_report.json \
  --reviewer human \
  --reason "Local LLM eval gates passed" \
  --decision APPROVE
```

Existing LLM commands accept `--llm-model-alias`; invalid, expired, wrong-role,
or unsafe aliases block instead of falling back. The LLM alias never submits
orders, reads secrets, changes risk, activates broker/live, or mutates
`models/latest_model.json`. Verify this path with
`scripts/verify-llm-supervision.sh`.

`evaluate-approved-data` is the reproducible offline eligibility gate for
approved Parquet packages. It requires `ohlcv.parquet`, `manifest.json`, and
`catalog_entry.json`; verifies the dataset hash from the manifest; validates
symbols and `1d`/`1h` timestamps against the configured universe; runs the
deterministic momentum/volatility-target backtest and logistic baseline
evaluation; compares against a majority-classifier baseline; and writes
`data_quality.json`, `backtest.json`, `backtest.md`, `model_run.json`,
`model_eval.json`, `walk_forward.json`, `regime_slices.json`,
`promotion_decision.json`, `evaluation_summary.json`, and
`evaluation_summary.md` under
`reports/tmp/approved_eval/<dataset_id>/<frequency>/<as_of_date>/`. Exit code
`0` means `eligible_for_paper_challenger=true`; exit code `1` means the package
was evaluated but blocked or rejected by gates; exit code `2` means an
operational problem such as missing Parquet dependencies, required files,
manifest errors, or hash mismatch. It does not read credentials, contact
brokers, replace `models/latest_model.json`, submit signals, or download data.
Challenger decisions include explicit costs/slippage/turnover evidence and
block obvious temporal leakage, non-robust walk-forward lift, too few trades,
excessive drawdown, or cost-adjusted negative candidates.
It is normally invoked through `prepare-paper-daily`; use it directly only for
manual diagnostics.
`model-research-sweep` consumes the same approved package. Its `--as-of-date`
must exactly match the approved dataset `manifest.json`/`catalog_entry.json`
`as_of_date`; a missing approved date blocks with
`missing_approved_dataset_as_of_date`, and a mismatch exits `2` before writing
artifacts with `approved_dataset_as_of_date_mismatch:<requested>:<approved>`.
Generated candidate specs keep the approved dataset `as_of_date` so
`evaluate-approved-data --candidate-spec` can validate them reproducibly.
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
`status=WARN` exits `0`; `status=CRITICAL` exits `1`; operational failures or
broker snapshot errors exit `2`. Critical alerts include observability
diagnostics for missing or invalid session artifacts, blocked sessions, blocked
executions, closeouts in `PENDING` or `UNMATCHED`, submitted executions without
closeout evidence, broker open orders without local closed closeout evidence,
and existing observability blockers. Warnings include missing requested ledgers,
no recent sessions, or incomplete non-critical evidence such as a ready session
without execution evidence. The monitor always writes a `stability` section:
`--min-stable-sessions` defaults to `60`, `stable_session_count` counts complete
paper sessions with `paper_session=READY`, execution `SUBMITTED`, closeout
`CLOSED`, and no associated diagnostic/blocker, and
`ready_for_live_review=true` is only a documentary marker for future manual
review; it does not authorize live trading. By default the monitor does not
contact Alpaca, read broker credentials, download data, retrain models,
recalculate signals, submit orders, cancel orders, or authorize live trading.
`paper-campaign-report` is the read-only campaign rollup for the paper-first
phase. It combines readiness reports, paper sessions, executions, closeouts,
monitor alerts, and optional JSONL ledgers into
`reports/tmp/paper_campaign/latest.json` plus
`reports/tmp/paper_campaign/latest.md` by default. It reports complete sessions,
pending sessions, blockers, latest readiness/session dates, progress against
the documentary monitor target, the 20-session `paper_auto_campaign`, the
60-session `stability_campaign`, and always keeps
`live_trading_authorized=false`. It
does not contact brokers, read credentials, send Telegram notifications,
download data, recalculate signals, or authorize live trading. If decision or
performance artifacts exist under `reports/tmp/paper_decisions` or
`reports/tmp/paper_performance`, the campaign report includes their latest
summaries without requiring them.

`paper-day-close` writes the auditable daily decision journal under
`reports/tmp/paper_decisions/<as_of_date>/decision.json` and `.md`. It reads
readiness, broker-run, monitor, and campaign artifacts, stores their paths and
SHA-256 hashes, maps monitor/campaign evidence to `CONTINUE`, `REVIEW`, `STOP`,
or `ERROR`, redacts secret-shaped text, and can append a redacted JSONL event.
It is paper-only and never contacts brokers or authorizes live trading.

`paper-performance-report` writes paper-only performance evidence under
`reports/tmp/paper_performance/latest.json` and `.md`. It reads existing
sessions, executions, closeouts, ledgers, an optional approved backtest report,
and optional `--broker-statement` JSON/CSV exported manually from the paper
broker. PnL remains `proxy` without a valid statement; a valid statement marks
PnL as `broker_statement` and reports missing fill, quantity, price, symbol, or
date differences. Performance stability also requires an approved backtest and
the explicit sample floor `--min-stable-sessions 60 --min-stable-fills 60` by
default; insufficient samples add warnings and keep
`paper_metrics.performance_stable=false`. This report is evidence only and does
not authorize live trading.

`paper-statement-validate` normalizes a manually exported local broker
statement before reconciliation. It accepts JSON or CSV, writes
`reports/tmp/paper_statements/<as_of_date>/statement.normalized.json` and `.md`,
requires `client_order_id`, `symbol`, `side`, `quantity`,
`filled_avg_price`, `filled_at`, and `realized_pnl`, rejects duplicate
`client_order_id`, accepts common broker CSV column aliases, warns on
timezone-free fills or fills outside `--as-of-date`, preserves extra fields
under redacted `raw`, and never connects to a broker or reads credentials.
`paper-performance-report
--broker-statement` accepts either the raw statement or this normalized output.

`paper-weekly-summary` writes the weekly paper campaign rollup under
`reports/tmp/paper_weekly_summary/<week>/`. It reads daily decisions,
performance, campaign reports, and ledgers; `CONTINUE` weeks produce `OK`, any
`STOP` produces `CRITICAL`, recurrent `REVIEW` produces `WARN`, and invalid
decision JSON returns `ERROR` with exit code `2`. JSON and Markdown are
redacted. Add `--history-weeks 4` to include blocker aging across recent weeks;
historical invalid JSON is reported as a warning, while current-week invalid
decision JSON remains `ERROR`.

`paper-ops-check` is the read-only daily completeness gate before the next
paper submit. It reads readiness, sessions, monitor, campaign, decision,
performance, and optional ledger evidence, then writes
`reports/tmp/paper_ops_check/<as_of_date>/ops_check.json` and `.md`. `OK`
requires `READY` readiness, non-critical monitor/campaign evidence,
`CONTINUE`, performance present, and no pending/unmatched closeouts. Missing
performance or `REVIEW` is `WARN`; `STOP`, critical monitor/campaign evidence,
or pending/unmatched closeouts is `CRITICAL`; unreadable required JSON is
`ERROR`.

`paper-session` also applies signal-quality gates from `configs/risk.yml`.
`min_signal_margin` requires the selected buy probability to clear the signal
threshold by a minimum margin, and `max_buy_signals` blocks broad market
"everything buys" days before an order intent is created. The signal report
keeps all raw model signals plus `model_provenance` so reviewers can inspect
the model hash, feature names, and missing descriptive metadata without
mutating `models/latest_model.json`.

The same signal report includes a paper-only `position_plan`. It classifies
broker positions as `HOLD` when the current signal still supports them,
`CLOSE` when their signal turns non-buy or the strategy rotates to another
selected symbol, and `OPEN` when the selected buy signal has no current
position. `paper-execute-session` recalculates this plan from live Alpaca paper
positions before submitting. Dynamic close orders require the extra
`--confirm-dynamic-position-actions` flag; without it the run writes evidence
and exits blocked instead of selling.

`paper-ops-rehearsal` creates a deterministic offline paper week under
`reports/tmp/paper_rehearsal/<as_of_date>/`. It writes local fixtures, validates
a statement, runs performance, ops check, weekly summary, and a defer
`model-review-decision`, then returns `OK`, `WARN`, `CRITICAL`, or `ERROR`.
Scenarios are `complete`, `missing-performance`, `stop`, and
`invalid-statement`; none reads broker credentials or submits orders.

`paper-evidence-index` writes
`reports/tmp/paper_evidence_index/<as_of_date>/evidence_index.json` and `.md`.
It indexes readiness, monitor, campaign, decision, performance, ops check,
weekly summary, statement, and challenger decision artifacts. Missing optional
artifacts are `WARN`; invalid required JSON is `ERROR`. It is read-only over
source artifacts and keeps live trading disabled.

`model-challenger-report` writes governance evidence under
`reports/tmp/model_challenger/`. It reads an approved-data evaluation directory,
optional paper performance, and optional MLflow review. It classifies a
candidate as `REVIEWABLE`, `REJECTED`, `BLOCKED`, or `ERROR`; it never mutates
`models/latest_model.json` and never replaces the champion automatically.

`model-review-decision` records human challenger review without promotion. It
writes `reports/tmp/model_challenger_decisions/<date>/decision.json` and `.md`,
records artifact hashes, and allows `APPROVE_FOR_NEXT_PAPER_CYCLE` only when
the challenger report is `REVIEWABLE`. `REJECT` and `DEFER` can record
`REVIEWABLE`, `REJECTED`, or `BLOCKED` reports. It never mutates
`models/latest_model.json`.

`model-review-cycle-report` reads a challenger report plus the recorded human
decision and writes `reports/tmp/model_challenger_cycles/<date>/cycle_report.*`.
It recommends `READY_FOR_NEXT_PAPER_CYCLE`, `REJECTED_NO_PROMOTION`, or
`DEFERRED`, includes artifact hashes, and still never mutates
`models/latest_model.json`.

`futures-readiness-report` is read-only preparation for future MES/MNQ work. It
loads `configs/futures_micro.yml` by default, validates contract placeholders
for calendar, roll, tick size/value, margin, sessions, and costs, reports the
research-only platform decision, and writes
`reports/tmp/futures_readiness/latest.json` plus `.md`. Missing platform
decision is `WARN`; missing contract readiness fields are `BLOCKED`. It does
not read IBKR credentials, build broker clients, or create futures
submit/cancel commands.

`futures-research-scaffold` creates only offline research manifests under
`reports/tmp/futures_research/<as_of_date>/`. It reuses futures readiness
evidence, emits contracts, tick values, margin placeholders, sessions, roll
rules, costs, and data requirements, returns `WARN` for a missing platform
decision and `BLOCKED` for missing contract requirements, and never reads
credentials or creates execution adapters.
The only broker access is the explicit read-only snapshot:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --as-of-date 2026-06-16 \
  --broker-read-only \
  --confirm-paper
```

That snapshot builds an Alpaca paper client from current process environment
variables only, reads account status/balances, allowlisted positions, and open
orders, and never sends, closes, or cancels orders.
Telegram notifications are disabled by default. Use `--telegram-dry-run` to
write a redacted preview without reading environment variables or making a
network call. Use `--send-telegram` to send one plain-text summary through the
Telegram Bot API `sendMessage`; the command reads only
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the current process
environment and never reads `.env`. By default it sends only critical alerts;
add `--telegram-send-warnings` to include warnings. Artifacts are written before
any Telegram attempt, and send failures exit `2` with redacted error details.

`paper-daily` is the daily paper-only operator for a manual cron/runbook. It
loads `configs/paper_daily.yml` by default, accepts CLI overrides for
`--source-csv`, `--from`, `--to`, `--as-of-date`, `--session-dir`,
`--sessions-root`, `--ledger-output`, `--output`, and `--markdown-output`, then
orchestrates existing functions without subprocesses: close prior submitted
executions discovered under `sessions_root` when explicitly confirmed, create
the offline `paper-session`, write observability and monitor reports, block
submits on critical or operational monitor evidence, submit at most one USD
`1.0` Alpaca paper order when explicitly confirmed, optionally close the new
evidence loop, and write final JSON/Markdown under
`reports/tmp/paper_daily/latest.*` by default. With no broker confirmations it
runs only offline steps and marks broker actions `SKIPPED`; no Alpaca client is
built and no broker environment variables are read. For an operational broker
paper run, use the readiness-confirmed wrapper against the approved
`readiness.json`; it requires the readiness confirmation plus all broker
confirmations, `--require-clean-state`, and forces Telegram off:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-daily-from-readiness --readiness reports/tmp/paper_daily_prepare/core_etfs/1d/2026-06-16/readiness.json --confirm-readiness --confirm-paper --confirm-auto-close --confirm-auto-submit --require-clean-state
```

`paper-daily-from-readiness` refuses to run unless `readiness.status=READY`,
`ready_for_paper_daily=true`, `exit_code=0`,
`offline_smoke.requested=true`, `offline_smoke.ran=true`, and
`offline_smoke.exit_code=0`, and the clean-state confirmation is present. It
writes `broker_run.json` and `broker_run.md` alongside the broker-confirmed
daily/session/observability/monitor artifacts.
Exit code `0` mirrors a successful `paper-daily`; `1` means the readiness gate,
paper gate, or final monitor blocked; `2` means a missing confirmation, invalid
readiness/config, monitor `ERROR`, or another operational error. Direct
`paper-daily` remains available for offline diagnostics and lower-level runbook
work; Telegram flags on direct `paper-daily` are still passed to the final
`paper-monitor` attempt. These
commands do not authorize live trading, read `.env`, download data, retrain or
replace models, recalculate signals outside the offline session package, cancel
orders, or remove old evidence. `--ledger-output` adds a redacted `paper_daily`
event only with paths, status, exit code, and summary reasons, not account
payloads, credentials, Telegram tokens, or full broker responses.
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
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor --sessions-root reports/tmp/paper_session --ledger-input reports/tmp/paper_observability/ledger.jsonl --output reports/tmp/paper_monitor/latest.json --markdown-output reports/tmp/paper_monitor/latest.md --min-stable-sessions 60 --ledger-output reports/tmp/paper_observability/ledger.jsonl
```
`train --model logistic-baseline` is a pure-Python Phase 3 baseline for temporal
evaluation plumbing only; it is not a champion model and does not authorize
trading.
`promote` only writes a champion/challenger eligibility decision. Approval means
eligible for paper challenger review, not live trading.
`llm-eval` runs local guardrail evals without network access. The legacy
Responses wrapper remains test-injectable for guardrail unit tests, but its
default runtime constructor raises because external LLM APIs are disabled.
`paper --kill-switch-test` proves that new paper orders are rejected while the
kill-switch is active and that cancellation remains available in dry-run mode.
`paper --submit-signal-order` converts the local baseline model's latest valid
feature rows into long/cash signals, selects the highest-probability `buy`
signal, and submits at most one governed-notional Alpaca paper order after risk
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
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --real-paper --confirm-paper --list-orders --order-status open --output reports/tmp/paper/open_orders.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --real-paper --confirm-paper --get-order --client-order-id signal-spy-20240329 --output reports/tmp/paper/order_signal_spy.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --real-paper --confirm-paper --reconcile-order --source-report reports/tmp/paper/latest.json --output reports/tmp/paper/reconciliation.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --real-paper --confirm-paper --cancel-order --client-order-id signal-spy-20240329 --confirm-cancel --output reports/tmp/paper/cancel_signal_spy.json
```

Install optional research dependencies only after reviewing
`docs/tooling-risk-register.md` and `configs/permissions.yml`.
