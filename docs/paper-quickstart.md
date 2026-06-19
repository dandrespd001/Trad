# Paper Trading Quickstart

This project is safe to operate as research and Alpaca paper trading only.
Live trading remains out of scope.

## 1. Install

```bash
python -m pip install -e ".[research,dev]"
```

Broker access is optional and only needed for confirmed Alpaca paper runs:

```bash
python -m pip install -e ".[broker]"
export ALPACA_PAPER_API_KEY="..."
export ALPACA_PAPER_SECRET_KEY="..."
```

Do not store broker credentials in repository files.

## 2. Verify the local environment

```bash
scripts/verify-paper-environment.sh
scripts/verify-release.sh
```

For a quick paper-only check:

```bash
scripts/verify-paper-focused.sh
scripts/verify-paper-gates.sh
```

## 3. Run an offline paper readiness smoke

Use explicit dates. Do not use `today` for reproducible runs.

```bash
PYTHONPATH=src python3 -m trading_ai.cli prepare-paper-daily \
  --approved-dir data/raw/approved/core_etfs/1d \
  --dataset-id core_etfs \
  --frequency 1d \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16 \
  --config configs/universe.yml \
  --risk configs/risk.yml \
  --signal-model models/latest_model.json \
  --run-offline-smoke
```

Expected result: readiness is `READY`, offline smoke ran, and no broker client
or credentials were used.

## 4. Run the safe auto wrapper

Evidence-only run:

```bash
scripts/run-paper-daily-safe.sh \
  --approved-dir data/raw/approved/core_etfs/1d \
  --dataset-id core_etfs \
  --frequency 1d \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16
```

Confirmed paper automation must be clean-state gated:

```bash
scripts/run-paper-daily-safe.sh \
  --approved-dir data/raw/approved/core_etfs/1d \
  --dataset-id core_etfs \
  --frequency 1d \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16 \
  --require-clean-state \
  --confirm-paper-auto
```

The wrapper rejects relative dates and refuses `--confirm-paper-auto` without
`--require-clean-state`.

## 5. Monitor, performance, and recovery

After broker-confirmed paper activity:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --output reports/tmp/paper_monitor/latest.json \
  --markdown-output reports/tmp/paper_monitor/latest.md \
  --as-of-date 2026-06-16

PYTHONPATH=src python3 -m trading_ai.cli paper-performance-report \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --min-stable-sessions 60 \
  --min-stable-fills 60

PYTHONPATH=src python3 -m trading_ai.cli paper-ops-check \
  --as-of-date 2026-06-16 \
  --readiness-root reports/tmp/paper_daily_prepare \
  --sessions-root reports/tmp/paper_session \
  --monitor-root reports/tmp/paper_monitor \
  --campaign-root reports/tmp/paper_campaign \
  --decisions-root reports/tmp/paper_decisions \
  --performance-root reports/tmp/paper_performance \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl
```

If monitor or ops check is `CRITICAL`, stop new paper submits and resolve the
blocker before the next cycle.

## 6. Local cleanup

Preview cache cleanup:

```bash
scripts/clean-local-artifacts.sh
```

Apply cleanup:

```bash
scripts/clean-local-artifacts.sh --apply
```

The cleanup script only removes local caches and ignored build metadata. It does
not remove reports, approved data, models, or source files.
