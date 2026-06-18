# Paper Operation Next Sprints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add paper weekly summaries, broker statement reconciliation, challenger governance reporting, and futures platform decision evidence without enabling live trading.

**Architecture:** Extend the existing `trading-ai` CLI and reporting modules under `src/trading_ai/execution/`, reusing artifact discovery, append-only outputs, status mapping, and redaction patterns already present in the paper reports. Add a small model governance module under `src/trading_ai/evaluation/` that reads existing evaluation artifacts and never mutates `models/latest_model.json`.

**Tech Stack:** Python 3.12, `argparse`, `unittest`, JSON/Markdown artifacts, existing YAML loader.

---

### Task 1: Paper Weekly Summary

**Files:**
- Create: `src/trading_ai/execution/paper_weekly_summary.py`
- Modify: `src/trading_ai/cli_paper.py`
- Modify: `src/trading_ai/cli.py`
- Test: `tests/test_paper_weekly_summary.py`

- [ ] **Step 1: Write failing CLI and behavior tests**

```python
args = build_parser().parse_args(["paper-weekly-summary"])
self.assertEqual(args.decisions_root, "reports/tmp/paper_decisions")
self.assertEqual(args.output_dir, "reports/tmp/paper_weekly_summary")
```

Cover five `CONTINUE` decisions returning `OK`, any `STOP` returning `CRITICAL`, repeated `REVIEW` returning `WARN`, invalid decision JSON returning exit `2`, and JSON/Markdown redaction.

- [ ] **Step 2: Verify tests fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_weekly_summary -v`

Expected: FAIL because `paper-weekly-summary` is not registered.

- [ ] **Step 3: Implement minimal report**

Create a module that discovers `decision.json`, paper performance JSON, campaign JSON, and ledger JSONL files; aggregates decision counts, recurrent blockers, sessions, fills, pending, unmatched, and warnings; writes `weekly_summary.json` and `weekly_summary.md` under `<output-dir>/<week>/`; redacts secret-like strings using `redact_secrets`; returns exit code `2` for invalid decision JSON.

- [ ] **Step 4: Verify tests pass**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_weekly_summary -v`

Expected: PASS.

### Task 2: Broker Statement Reconciliation

**Files:**
- Modify: `src/trading_ai/execution/paper_performance.py`
- Modify: `src/trading_ai/cli_paper.py`
- Modify: `src/trading_ai/cli.py`
- Test: `tests/test_paper_performance_report.py`

- [ ] **Step 1: Write failing tests**

```python
args = build_parser().parse_args(["paper-performance-report", "--broker-statement", "statement.json"])
self.assertEqual(args.broker_statement, "statement.json")
```

Cover valid statement switching PnL source to `broker_statement`, missing statement keeping `proxy` with warning, unmatched local fill producing a blocker, invalid statement returning `ERROR` exit `2`, and live trading remaining disabled.

- [ ] **Step 2: Verify tests fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_performance_report -v`

Expected: FAIL because `--broker-statement` is unsupported.

- [ ] **Step 3: Implement minimal reconciliation**

Accept optional CSV or JSON statement files. Normalize rows with `client_order_id`, `symbol`, `side`, `quantity`, `filled_avg_price`, `filled_at` or compatible aliases. Match local closed closeouts against statement rows and report missing fill, quantity mismatch, price mismatch, symbol mismatch, and date mismatch. Operational parse/schema errors return `PaperPerformanceReportResult(exit_code=2, status="ERROR", ...)`.

- [ ] **Step 4: Verify tests pass**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_performance_report -v`

Expected: PASS.

### Task 3: Champion/Challenger Governance Report

**Files:**
- Create: `src/trading_ai/evaluation/model_challenger.py`
- Modify: `src/trading_ai/cli.py`
- Test: `tests/test_model_challenger_report.py`

- [ ] **Step 1: Write failing tests**

```python
args = build_parser().parse_args(["model-challenger-report", "--evaluation-dir", "run"])
self.assertEqual(args.output_dir, "reports/tmp/model_challenger")
```

Cover robust candidate plus compatible paper performance returning `REVIEWABLE`, leakage/cost/drawdown reasons returning `REJECTED`, missing required artifacts returning `ERROR`, missing MLflow review remaining optional, and unchanged `models/latest_model.json`.

- [ ] **Step 2: Verify tests fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_model_challenger_report -v`

Expected: FAIL because `model-challenger-report` is not registered.

- [ ] **Step 3: Implement minimal report**

Read `evaluation_summary.json`, `promotion_decision.json`, `walk_forward.json`, `regime_slices.json`, optional MLflow review JSON, and optional paper performance JSON. Classify `REVIEWABLE`, `REJECTED`, `BLOCKED`, or `ERROR`; require OOS evidence, cost-aware profitability, sufficient trades, no leakage, tolerable drawdown, and compatible paper performance. Always include authority fields showing no automatic champion replacement and no latest-model mutation.

- [ ] **Step 4: Verify tests pass**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_model_challenger_report -v`

Expected: PASS.

### Task 4: Futures Platform Decision

**Files:**
- Modify: `src/trading_ai/execution/futures_readiness.py`
- Modify: `configs/futures_micro.yml`
- Create: `docs/futures-micro-platform-decision.md`
- Test: `tests/test_futures_readiness_report.py`

- [ ] **Step 1: Write failing tests**

```python
self.assertIn("platform_decision", payload)
self.assertEqual(payload["platform_decision"]["selected"], "LEAN_IBKR_RESEARCH_ONLY")
```

Cover complete MES/MNQ config returning `OK`, missing platform decision returning `WARN`, missing calendar/roll/costs/margin returning `BLOCKED`, no futures execution commands, and `live_trading_allowed=false`.

- [ ] **Step 2: Verify tests fail**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_futures_readiness_report -v`

Expected: FAIL because `platform_decision` is absent.

- [ ] **Step 3: Implement minimal readiness extension**

Parse `futures.platform_decision`; return `WARN` with no technical block when absent, `BLOCKED` for missing contract requirements, and `OK` when contracts and platform decision are complete. Render the decision in Markdown and keep safety fields read-only.

- [ ] **Step 4: Verify tests pass**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_futures_readiness_report -v`

Expected: PASS.

### Task 5: Documentation And Gates

**Files:**
- Modify: `docs/paper-real-runbook.md`
- Possibly modify: `README.md`

- [ ] **Step 1: Add operational checklist**

Document the daily paper sequence: `prepare-paper-daily`, `paper-daily-from-readiness`, `paper-monitor`, `paper-campaign-report`, `paper-day-close`, `paper-performance-report`, and the short `CONTINUE`/`REVIEW`/`STOP`/`ERROR` checklist.

- [ ] **Step 2: Verify local gates**

Run: `./scripts/verify-paper-gates.sh`
Run: `git diff --check`
Run: `git diff -- models/latest_model.json`
Run: `rg "live_trading_(authorized|allowed)=true|futures-(execute|submit)" .`

Expected: paper gates and whitespace checks pass, `models/latest_model.json` has no diff, and no live/futures execution authorization strings are introduced.
