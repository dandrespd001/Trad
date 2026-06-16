# Trading AI Research MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `docs/trading-bot-ai-research.md` into a runnable Phase 1 research scaffold with documented risk gates, permissions, baseline metrics, and a benchmark notebook.

**Architecture:** Keep the first implementation broker-free and deterministic. Core Python modules expose small research/risk primitives that can be tested with the standard library; the notebook consumes those primitives and leaves heavier ML/forecasting dependencies as documented optional extensions.

**Tech Stack:** Python 3.11+, stdlib tests, optional pandas/polars/scikit-learn/LightGBM/XGBoost/MLflow/Evidently/Optuna for future Phase 1 execution.

---

### Task 1: Project Skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `src/trading_ai/__init__.py`
- Create: `src/trading_ai/research/__init__.py`
- Create: `src/trading_ai/risk/__init__.py`

- [x] **Step 1: Create package metadata and dependencies**

Define the project as a Python package named `trading-ai-research` with optional dependency groups for research, ML, monitoring, and forecasting.

- [x] **Step 2: Create a concise README**

Document that this is a paper/research-only scaffold and that live broker execution is out of scope.

### Task 2: Policy And Permission Artifacts

**Files:**
- Create: `configs/permissions.yml`
- Create: `docs/tooling-risk-register.md`
- Create: `docs/model-evaluation-policy.md`

- [x] **Step 1: Write permissions policy**

Separate `read_only`, `paper_only`, and `live_prohibited` capabilities. Keep broker-live and secrets access prohibited by default.

- [x] **Step 2: Write tooling risk register**

Capture the approved tools from the research document, risk level, allowed phase, and control required before installation or use.

- [x] **Step 3: Write model evaluation policy**

Define no-leakage, walk-forward, cost/slippage, champion/challenger, paper-trading, and live gates.

### Task 3: TDD Research Primitives

**Files:**
- Create: `tests/test_research_metrics.py`
- Create: `tests/test_risk_policy.py`
- Create: `src/trading_ai/research/metrics.py`
- Create: `src/trading_ai/risk/policy.py`

- [x] **Step 1: Write failing tests for metrics and gates**

Tests cover cumulative return, max drawdown, annualized Sharpe, volatility-target sizing, max daily loss, max drawdown, and live-trading prohibition.

- [x] **Step 2: Run tests and verify the expected import failures**

Run: `python3 -m unittest discover -s tests -v`

- [x] **Step 3: Implement minimal research and risk modules**

Implement pure-Python functions with no broker, network, data-provider, or ML dependency.

- [x] **Step 4: Run tests and verify they pass**

Run: `python3 -m unittest discover -s tests -v`

### Task 4: Benchmark Notebook

**Files:**
- Create: `notebooks/benchmark_baseline_vs_ml_vs_timeseries.ipynb`

- [x] **Step 1: Scaffold notebook from the local Jupyter template**

Run the `jupyter-notebook` skill helper with `--kind experiment`.

- [x] **Step 2: Replace template cells with the research benchmark flow**

Include objective, reproducibility setup, synthetic OHLCV sample, baseline metrics, ML/forecasting extension hooks, and next steps.

- [x] **Step 3: Validate notebook JSON and, if dependencies allow, execute lightweight cells**

Run a JSON parse and compile code cells. Full execution is optional until notebook dependencies are installed.

### Task 5: Research Document Update

**Files:**
- Modify: `docs/trading-bot-ai-research.md`

- [x] **Step 1: Add an implementation status section**

Record the created artifacts, current scope, and next commands.

- [x] **Step 2: Add source-validation note**

Record that the initial implementation keeps current-source-sensitive facts in docs and does not encode them as trading assumptions.
