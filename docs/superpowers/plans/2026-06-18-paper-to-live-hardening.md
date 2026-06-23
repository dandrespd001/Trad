# Paper-To-Live Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot reliable for multi-day broker-confirmed paper trials, then add a tightly gated path toward real-money readiness without weakening the current paper-only safety boundary.

**Architecture:** Keep the existing artifact-first CLI design: JSON/Markdown evidence, append-only ledgers, explicit confirmations, deterministic gates, and LLM authority set to none. First close paper reliability gaps; only after clean paper evidence exists, add separate live-readiness artifacts and a manual canary path instead of changing the paper execution path in place.

**Tech Stack:** Python 3, argparse CLI, unittest, JSON/Markdown artifacts, local logistic baseline models, Alpaca paper broker adapter, optional OpenAI review/supervision with guardrails.

---

## Audit Snapshot

Date: 2026-06-18

Verified locally:

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v`
- `./scripts/verify-paper-focused.sh`
- `./scripts/verify-paper-gates.sh`
- `./scripts/verify-adaptive-routing.sh`
- `./scripts/verify-llm-supervision.sh`
- `./scripts/verify-adaptive-training.sh`
- `git diff -- models/latest_model.json`
- comprobar que no aparece autorización live con valores `true` en texto o código.
- `rg 'subparsers\.add_parser\("futures-(execute|submit)"' src tests`

Result: unit tests and gates passed; no `models/latest_model.json` diff; no live authorization strings; no futures execute/submit CLI parser.

Current state:

- Strong for research and paper-only operation.
- Good safety posture: confirmations, LLM read-only authority, paper-only Alpaca client, fixed paper notional, artifact hashes, redaction, rehearsals, and no silent fallback on adaptive routing.
- Not ready for real money yet. There is no separate live-readiness contract, no live canary adapter, and not enough enforced multi-day paper evidence in the confirmed path.

## Findings

### F1: Manual `paper-session -> paper-execute-session` path can break on relative config paths

Impact: High for operator usability. `paper-session` stores `inputs.config`, `inputs.risk`, and `inputs.signal_model` exactly as passed. `paper-execute-session` resolves relative values against `session_dir`, so a manually created session with default `configs/universe.yml` can later fail because it looks for `<session_dir>/configs/universe.yml`.

References:

- `src/trading_ai/execution/paper_session.py:363`
- `src/trading_ai/execution/paper_execute_session.py:296`

### F2: Paper model alias routing validates hash but not active model schema/provenance

Impact: High for adaptive routing. `resolve_paper_model_route` checks alias state, expiry, file existence, hash, and basic safety, but does not load and validate the active logistic model before routing. A forged alias with a matching hash could route to a malformed model and fail later or produce unauditable behavior.

References:

- `src/trading_ai/execution/paper_model_alias.py:76`
- `src/trading_ai/execution/paper_model_alias.py:107`

### F3: Shadow outcome ledger omits `NO_SHADOW_SIGNAL` and `BLOCKED` days

Impact: Medium-high for challenger evidence. The report writes daily JSON for all states, but appends to the ledger only for `RECORDED`. The scorecard computes missing outcome rate from ledger records, so missing or blocked days can disappear from the evidence denominator.

References:

- `src/trading_ai/execution/paper_shadow_outcome.py:49`
- `src/trading_ai/execution/paper_shadow_outcome.py:56`
- `src/trading_ai/execution/paper_shadow_scorecard.py:91`

### F4: Confirmed auto-cycle duplicate and clean-state gates are optional

Impact: Medium-high for multi-day paper trials. `paper-auto-cycle --confirm-paper-auto` can run without `--require-clean-state`; duplicate confirmed cycle checks are bypassed when that flag is omitted. This is acceptable for local rehearsals, but too loose for broker-confirmed trials.

References:

- `src/trading_ai/cli_paper.py:500`
- `src/trading_ai/execution/paper_auto_cycle.py:110`
- `src/trading_ai/execution/paper_auto_cycle.py:741`
- `scripts/run-paper-auto-cycle.sh:11`

### F5: Sizing and risk are intentionally fixed for paper, but there is no graduation policy

Impact: Medium (partially resolved). `paper`/session notional is now sourced from `risk_limits.paper_notional_usd`, preserving USD 1.0 as default while allowing explicit operator-authored scale steps. Remaining work is a formal multi-stage graduation policy for clean transitions (paper canary -> paper scale-up -> live-readiness).

References:

- `src/trading_ai/execution/paper_execute_session.py:24`
- `src/trading_ai/execution/paper_execute_session.py:267`

### F6: No live-readiness artifact exists

Impact: Medium. The current architecture correctly prohibits live trading. To move toward real money without ad hoc changes, add a separate live-readiness report that proves paper evidence, broker reconciliation, risk caps, operator approval, and secrets handling before any live command exists.

## Sprint 83: Fix Manual Session Path Reproducibility

**Files:**

- Modify: `src/trading_ai/execution/paper_session.py`
- Modify: `src/trading_ai/execution/paper_execute_session.py`
- Test: `tests/test_paper_execute_session.py`
- Test: `tests/test_paper_session.py`

- [x] **Step 1: Add failing regression test for relative session inputs**

Add a test that creates a session with relative `configs/universe.yml`, `configs/risk.yml`, and `models/latest_model.json`, then verifies `paper-execute-session` can resolve those paths from the workspace.

Expected failing assertion before the fix:

```python
self.assertNotIn("session.inputs.config does not exist", str(result.reasons))
```

- [x] **Step 2: Store resolved absolute paths in `paper-session` payload**

In `_build_session_payload`, write `Path(...).expanduser().resolve(strict=False)` for `source_csv`, `config`, `risk`, and `signal_model`.

Expected shape:

```python
"inputs": {
    "source_csv": str(Path(source_csv).expanduser().resolve(strict=False)),
    "config": str(Path(config).expanduser().resolve(strict=False)),
    "risk": str(Path(risk).expanduser().resolve(strict=False)),
    "signal_model": str(Path(signal_model).expanduser().resolve(strict=False)),
    "from": start,
    "to": end,
}
```

- [x] **Step 3: Add compatibility fallback in `paper-execute-session`**

In `_resolve_session_path`, if `session_dir / value` does not exist, also try `Path.cwd() / value` before failing. This preserves old session artifacts.

- [x] **Step 4: Verify**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_execute_session tests.test_paper_session -v
```

Expected: tests pass and manual session execution no longer depends on copying config files into the session directory.

## Sprint 84: Harden Paper Alias Model Validation

**Files:**

- Modify: `src/trading_ai/execution/paper_model_alias.py`
- Modify: `src/trading_ai/models/baseline.py`
- Test: `tests/test_paper_adaptive_routing.py`

- [x] **Step 1: Add failing tests for forged or invalid alias models**

Cases:

- `active_model_path` points to JSON missing `coefficients`.
- `coefficients` length differs from `feature_names`.
- non-finite values such as `NaN`, `Infinity`, or strings that cannot parse to finite floats.
- alias has `alias_state=ACTIVE_PAPER_ALIAS` but lacks human governance fields.

Expected route:

```python
self.assertEqual(route["route_state"], "BLOCKED")
self.assertIn(route["reason"], {"alias_model_invalid", "alias_governance_invalid"})
```

- [x] **Step 2: Add a model validation helper**

Add a small helper near `LogisticBaselineModel.from_dict` or inside `paper_model_alias.py`:

```python
def validate_logistic_model_payload(payload: Mapping[str, object]) -> None:
    model = LogisticBaselineModel.from_dict(payload)
    if len(model.coefficients) != len(model.feature_names):
        raise ValueError("coefficients length must match feature_names length")
    values = [model.intercept, *model.coefficients]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("model weights must be finite")
```

- [x] **Step 3: Validate route-time model and alias governance**

In `resolve_paper_model_route`, after hash verification:

- load the model JSON;
- validate it;
- require `reviewer`, `reason`, `candidate_model_run`;
- require `latest_model.mutated is False`;
- require `authority.mutates_latest_model is False`;
- require `authority.llm_authority in {"none", "", None}`.

- [x] **Step 4: Verify**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_adaptive_routing -v
./scripts/verify-adaptive-routing.sh
```

Expected: invalid aliases block before `prepare-paper-daily` or `paper-auto-cycle` can route through them.

## Sprint 85: Make Shadow Outcome Evidence Complete

**Files:**

- Modify: `src/trading_ai/execution/paper_shadow_outcome.py`
- Modify: `src/trading_ai/execution/paper_shadow_scorecard.py`
- Test: `tests/test_paper_adaptive_routing.py`

- [x] **Step 1: Add failing tests for omitted shadow days**

Create a ledger with:

- one `RECORDED` outcome;
- one `NO_SHADOW_SIGNAL`;
- one `BLOCKED` missing price outcome.

Expected:

```python
self.assertEqual(payload["metrics"]["record_count"], 3)
self.assertGreater(payload["metrics"]["missing_outcome_rate_pct"], 0.0)
```

- [x] **Step 2: Append every daily shadow state**

Change `run_paper_shadow_outcome_report` so `_write(..., append=True)` is used for `RECORDED`, `NO_SHADOW_SIGNAL`, and `BLOCKED`.

- [x] **Step 3: Add ledger idempotency**

Add `record_id = f"{as_of_date}:{horizon_days}:{symbol_or_none}"` to `_ledger_record`. Before appending, rewrite the JSONL file with any previous record for the same `record_id` removed, then append the new record.

- [x] **Step 4: Fix scorecard metrics**

Expose:

- `record_count`;
- `shadow_signal_count`;
- `outcome_count`;
- `blocked_outcome_count`;
- `no_shadow_signal_count`;
- `missing_outcome_rate_pct = blocked_outcome_count / max(1, shadow_signal_count + blocked_outcome_count) * 100`.

Do not count `NO_SHADOW_SIGNAL` as a missing outcome, but keep it in the audit trail.

- [x] **Step 5: Verify**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_adaptive_routing -v
./scripts/verify-adaptive-training.sh
```

Expected: a challenger cannot become alias-ready by silently dropping blocked outcome days.

## Sprint 86: Require Clean State For Confirmed Paper Automation

**Files:**

- Modify: `src/trading_ai/execution/paper_auto_cycle.py`
- Modify: `scripts/run-paper-auto-cycle.sh`
- Test: `tests/test_paper_auto_cycle.py`

- [x] **Step 1: Add failing test for confirmed auto without clean-state**

Call `run_paper_auto_cycle(confirm_paper_auto=True, require_clean_state=False, ...)`.

Expected:

```python
self.assertEqual(result.state, "BLOCKED")
self.assertIn("require_clean_state_required", result.payload["reasons"])
```

- [x] **Step 2: Block confirmed cycles unless clean-state is explicit**

At the start of `run_paper_auto_cycle`, if `confirm_paper_auto is True and require_clean_state is False`, write a blocked cycle before prepare:

```python
reasons=["require_clean_state_required"]
```

- [x] **Step 3: Keep evidence-only operation unchanged**

Do not require clean state when `confirm_paper_auto=False`; evidence-only runs should still be easy to run.

- [x] **Step 4: Update script help/runbook**

Document that broker-confirmed automation requires:

```bash
./scripts/run-paper-auto-cycle.sh --confirm-paper-auto --require-clean-state ...
```

- [x] **Step 5: Verify**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_auto_cycle -v
./scripts/verify-paper-gates.sh
```

Expected: duplicate-cycle and clean-state checks cannot be accidentally skipped in confirmed paper automation.

## Sprint 87: Add Paper Trial Day Report

**Files:**

- Create: `src/trading_ai/execution/paper_trial_day.py`
- Modify: `src/trading_ai/cli.py`
- Modify: `src/trading_ai/cli_paper.py`
- Test: `tests/test_paper_trial_day.py`
- Docs: `docs/paper-real-runbook.md`

- [x] **Step 1: Add CLI parser**

New public interface:

```bash
paper-trial-day --as-of-date YYYY-MM-DD \
  --cycle reports/tmp/paper_auto_cycle/YYYY-MM-DD/cycle.json \
  --monitor reports/tmp/paper_monitor/latest.json \
  --performance reports/tmp/paper_performance/latest.json \
  --shadow-outcome reports/tmp/paper_shadow/YYYY-MM-DD/shadow_outcome.json \
  --output-dir reports/tmp/paper_trial_day
```

- [x] **Step 2: Implement status contract**

States:

- `TRIAL_DAY_OK`: no open orders, no unresolved fills, no critical monitor, no blocked cycle.
- `TRIAL_DAY_WARN`: evidence-only, no trade, or non-critical warnings.
- `RECOVERY_REQUIRED`: pending closeout, statement mismatch, existing position, duplicate cycle, blocked outcome.
- `ERROR`: malformed required artifact.

- [x] **Step 3: Write JSON/Markdown**

Required output:

```json
{
  "schema_version": "1.0",
  "as_of_date": "2026-06-16",
  "trial_state": "TRIAL_DAY_OK",
  "blockers": [],
  "recoveries": [],
  "artifacts": {},
  "safety": {
    "paper_only": true,
    "live_trading_authorized": false,
    "orders_submitted": false
  }
}
```

- [x] **Step 4: Verify**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_trial_day -v
```

Expected: each day has one compact artifact that tells the operator whether the next day may proceed.

## Sprint 88: Paper Trial Campaign Gate For Real-Money Consideration

**Files:**

- Modify: `src/trading_ai/execution/paper_phase_review.py`
- Modify: `src/trading_ai/execution/paper_campaign.py`
- Test: `tests/test_paper_phase_review_report.py`
- Test: `tests/test_paper_campaign_report.py`

- [x] **Step 1: Add gate inputs for trial-day reports**

Add optional `--trial-day-root` to phase review and campaign report.

- [x] **Step 2: Add real-money consideration state**

New field:

```json
"real_money_consideration": {
  "state": "NOT_READY|PAPER_EVIDENCE_READY",
  "clean_broker_days": 0,
  "required_clean_broker_days": 20,
  "blockers": []
}
```

- [x] **Step 3: Require evidence before live-readiness**

Minimum gates:

- at least 20 clean broker-confirmed paper days;
- zero `RECOVERY_REQUIRED` days unresolved;
- zero statement mismatch;
- zero duplicate confirmed cycles;
- zero live authorization;
- no critical drift or performance report;
- shadow alias, if used, must be active and non-expired.

- [x] **Step 4: Verify**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_phase_review_report tests.test_paper_campaign_report -v
./scripts/verify-paper-focused.sh
```

Expected: paper can be called operationally stable only when multi-day broker evidence is clean.

## Sprint 89: Create Live Readiness Report Without Live Execution

**Files:**

- Create: `src/trading_ai/execution/live_readiness.py`
- Modify: `src/trading_ai/cli.py`
- Modify: `src/trading_ai/cli_paper.py`
- Test: `tests/test_live_readiness.py`
- Docs: `docs/paper-real-runbook.md`
- Config: `configs/permissions.yml`

- [x] **Step 1: Add `live-readiness-report` CLI**

Inputs:

```bash
live-readiness-report \
  --as-of-date YYYY-MM-DD \
  --phase-review reports/tmp/paper_phase_review/YYYY-MM-DD/phase_review.json \
  --campaign-report reports/tmp/paper_campaign/latest.json \
  --performance reports/tmp/paper_performance/latest.json \
  --permissions configs/permissions.yml \
  --reviewer NAME \
  --reason TEXT \
  --output-dir reports/tmp/live_readiness
```

- [x] **Step 2: Keep permissions default live-prohibited**

The report may output `READY_FOR_LIVE_CANARY`, but it must not edit `configs/risk.yml` or `configs/permissions.yml`.

- [x] **Step 3: Gate conditions**

Block unless:

- `real_money_consideration.state == PAPER_EVIDENCE_READY`;
- paper performance is not critical;
- no unresolved broker state;
- reviewer and reason are non-empty;
- permissions still show current live prohibition, proving this report does not enable live by itself.

- [x] **Step 4: Verify**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_readiness -v
rg "live_trading_authorized[=]true|live_trading_allowed[=]true" .
```

Expected: live-readiness is an evidence artifact only, not an execution capability.

## Sprint 90: Add Manual Live Canary Execution Behind Separate Gates

**Files:**

- Create: `src/trading_ai/execution/live_alpaca.py`
- Create: `src/trading_ai/execution/live_execute_session.py`
- Modify: `src/trading_ai/cli.py`
- Modify: `configs/permissions.yml`
- Test: `tests/test_live_execute_session.py`
- Docs: `docs/paper-real-runbook.md`

- [ ] **Step 1: Keep live adapter separate from paper adapter**

Do not add live flags to `AlpacaPaperBroker`. Create a new boundary that requires a live-specific client builder and explicit `paper=False`.

- [ ] **Step 2: Add canary-only limits**

Initial hardcoded defaults:

- max one live order per day;
- max USD 1 notional;
- buy-only unless separately approved;
- no LLM authority;
- no auto-cycle live path;
- no futures.

- [ ] **Step 3: Require all confirmations**

CLI must require:

```bash
--confirm-live-readiness --confirm-live --confirm-submit --reviewer NAME --reason TEXT
```

- [ ] **Step 4: Block unless live-readiness artifact is ready**

`live_execute_session` must read `live_readiness.json` and block unless `state == READY_FOR_LIVE_CANARY`.

- [ ] **Step 5: Verify**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_execute_session -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Expected: live execution exists only as a manual canary, with a separate evidence chain and separate tests.

## Sprint 91: CI/Gate Consolidation

**Files:**

- Create: `scripts/verify-trading-bot-release.sh`
- Modify: `.github/workflows/paper-gates.yml` if present
- Test: existing gate scripts

- [ ] **Step 1: Add release gate script**

Script commands:

```bash
./scripts/verify-paper-focused.sh
./scripts/verify-paper-gates.sh
./scripts/verify-adaptive-routing.sh
./scripts/verify-adaptive-training.sh
./scripts/verify-llm-supervision.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
git diff -- models/latest_model.json
rg "live_trading_authorized[=]true|live_trading_allowed[=]true" .
rg 'subparsers\.add_parser\("futures-(execute|submit)"' src tests
```

The two `rg` commands are expected to return no matches.

- [ ] **Step 2: Add mode-specific allowlist if Sprint 90 exists**

If live canary is implemented, replace the generic live-string gate with a stricter parser/report gate that allows only the new live-readiness/live-canary files.

- [ ] **Step 3: Verify**

Run:

```bash
./scripts/verify-trading-bot-release.sh
```

Expected: one command verifies the project is safe to operate in the current phase.

## Sprint 92: Multi-Day Paper Trial Runbook

**Files:**

- Modify: `docs/paper-real-runbook.md`
- Modify: `README.md`

- [ ] **Step 1: Define daily paper routine**

Daily before market:

```bash
paper-operator-status --as-of-date YYYY-MM-DD ...
./scripts/run-paper-auto-cycle.sh --as-of-date YYYY-MM-DD --approved-dir ... --from ... --to ... --require-clean-state
```

Daily after market:

```bash
paper-monitor --broker-read-only --confirm-paper ...
paper-performance-report ...
paper-shadow-outcome-report ...
paper-trial-day ...
```

- [ ] **Step 2: Define graduation gates**

Paper-only can run with evidence-only cycles immediately.

Broker-confirmed paper requires:

- clean operator status;
- confirmed paper credentials;
- canary notional cap from `risk.yml` (`paper_notional_usd`);
- no open orders or positions;
- no duplicate cycle;
- daily trial report.

Live canary requires:

- `PAPER_EVIDENCE_READY`;
- `READY_FOR_LIVE_CANARY`;
- explicit reviewer/reason;
- canary cap unchanged at USD 1;
- no LLM order authority.

- [ ] **Step 3: Verify docs commands**

Run command parser smoke tests for every documented CLI command with `--help` or test fixtures.

Expected: the runbook is executable without guessing missing artifacts.

## Sprint 93: Formalizar política de salida de paper

**Files:**

- Modify: `src/trading_ai/execution/paper_session.py`
- Modify: `src/trading_ai/execution/paper_execute_session.py`
- Modify: `src/trading_ai/execution/paper_trial_day.py`
- Modify: `src/trading_ai/execution/paper_phase_review.py`
- Modify: `src/trading_ai/execution/paper_campaign.py`
- Modify: `tests/test_paper_execute_session.py`
- Modify: `tests/test_paper_trial_day.py`
- Modify: `tests/test_paper_phase_review_report.py`
- Modify: `tests/test_paper_campaign_report.py`

- [ ] **Step 1: Definir etapas y límites de notional canary**

Agregar estado explícito de escalado (por ejemplo: `CANARY` -> `SCALE_UP` -> `READINESS`) y validar que:

- `CANARY`: `paper_notional_usd = 1.0`;
- `SCALE_UP`: cambio solo si se cumple la secuencia de días limpios en `paper_trial_day`;
- transición exige evidencia de `PAPER_EVIDENCE_READY`.

- [ ] **Step 2: Guardar trazabilidad de etapa en artefactos**

Incluir en `paper-trial-day`, `paper-phase-review` y `paper-campaign`:

- `paper_notional_usd` usado;
- etapa alcanzada;
- aprobador/revisión humana de cambio de etapa;
- bloqueos por regresión o salto de etapa no autorizado.

- [ ] **Step 3: Validar en ejecución**

`paper-execute-session` debe rechazar señales que no usen el `paper_notional_usd` de la etapa vigente.

- [ ] **Step 4: Verificar**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_execute_session tests.test_paper_trial_day tests.test_paper_phase_review_report tests.test_paper_campaign_report -v
```

Resultado esperado: la escalada de notional queda gobernada por evidencia + autorización explícita, sin permitir saltos de etapa.

## Execution Order

Recommended order:

1. Sprint 83
2. Sprint 84
3. Sprint 85
4. Sprint 86
5. Sprint 87
6. Sprint 88
7. Sprint 89
8. Stop for human review before Sprint 90
9. Sprint 90 only after paper evidence is clean
10. Sprint 91 and Sprint 92
11. Sprint 93 (formalize paper notional graduation)

Do not start live execution work until Sprints 83-89 are implemented and the release gate is green.
