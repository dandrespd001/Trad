# Pre-Live Fix Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cerrar los bloqueantes detectados en la auditoria pre-live antes de cualquier canary real USD 1.

**Architecture:** Mantener `live_readiness` como evidencia y no autorizacion. Separar gates dry-run de submit real: rollback CLI debe existir antes de submit, el broker live debe construirse solo dentro del wrapper S12, y el release gate debe medir cobertura real o declarar explicitamente que no lo hace.

**Tech Stack:** Python 3.12, `unittest`, shell gates, Alpaca boundary fake-only en tests, Markdown runbooks.

---

### Task 1: Restaurar un coverage gate real

**Files:**
- Modify: `pytest.py`
- Modify: `scripts/verify-release.sh`
- Test: `tests/test_paper_gate_scripts.py`

- [ ] **Step 1: Escribir test de gate**

Agregar un test que verifique que el coverage gate no puede ser satisfecho por el shim `pytest.py`. El test debe inspeccionar `scripts/verify-release.sh` y exigir una de estas dos formas: `coverage run -m unittest` o un path que invoque pytest real sin resolver el shim local.

- [ ] **Step 2: Ejecutar test y confirmar fallo**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_gate_scripts -v
```

Expected: FAIL hasta corregir el gate.

- [ ] **Step 3: Cambiar coverage gate**

Preferencia: reemplazar `python -m pytest --cov...` por:

```bash
coverage run -m unittest discover -s tests
coverage report --fail-under=75
```

Si se usa `.venv312/bin/python`, invocar `"$PYTHON_BIN" -m coverage ...`.

- [ ] **Step 4: Ajustar shim pytest**

El shim puede seguir existiendo para imports, pero `python -m pytest --version` no debe simular pytest real ni ejecutar la suite completa silenciosamente. Debe fallar con mensaje claro si pytest no esta instalado, o delegar al pytest real por ruta segura.

- [ ] **Step 5: Verificar**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_gate_scripts -v
bash scripts/verify-release.sh
```

Expected: coverage real ejecutado y release gate verde.

### Task 2: Registrar `live-safe-flatten` CLI y alinear rollback command

**Files:**
- Modify: `src/trading_ai/cli.py`
- Modify: `src/trading_ai/execution/live_canary.py`
- Test: `tests/test_live_safe_flatten.py`
- Test: `tests/test_run_live_canary.py`

- [ ] **Step 1: Escribir test CLI**

Agregar test que llame `main(["live-safe-flatten", ...])` con fake broker o con modo `--dry-run-fixture` si se agrega. El resultado debe escribir JSON/MD y mantener `orders_submitted=false`.

- [ ] **Step 2: Ejecutar test y confirmar fallo**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_safe_flatten tests.test_run_live_canary -v
```

Expected: FAIL porque el subcomando no existe.

- [ ] **Step 3: Implementar parser**

Registrar `live-safe-flatten` en `src/trading_ai/cli.py` con parametros minimos:

```text
--as-of-date
--positions-fixture
--allowlist
--reviewer
--reason
--output-dir
```

El CLI debe construir un fake/read-only broker desde fixture local, nunca cliente Alpaca live.

- [ ] **Step 4: Alinear `ROLLBACK_COMMAND`**

Actualizar `ROLLBACK_COMMAND` para incluir los flags reales que el operador debe completar o documentar placeholders seguros. El comando no debe prometer submit real.

- [ ] **Step 5: Verificar**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_safe_flatten tests.test_run_live_canary -v
PYTHONPATH=src python3 -m trading_ai.cli live-safe-flatten --help
python3 scripts/verify-safety-patterns.py --mode live
```

Expected: CLI existe, rollback dry-run genera evidencia y scanner live sigue limpio.

### Task 3: Decidir y corregir el estado S12

**Files:**
- Modify: `docs/live-transition-sprints.md`
- Modify: `docs/paper-real-runbook.md`
- Optionally modify: `src/trading_ai/cli.py`, `src/trading_ai/execution/live_canary.py`
- Test: `tests/test_run_live_canary.py`

- [ ] **Step 1: Elegir modo**

Usar una de estas opciones y documentarla:

```text
Opcion A: S12 queda dry-run/fake-only; canary real pendiente.
Opcion B: S12 conecta broker live real bajo wrapper humano.
```

Default recomendado: Opcion A hasta cerrar market-open y price sanity live.

- [ ] **Step 2: Si Opcion A**

Quitar o bloquear `--enable-real-submit` del wrapper por ahora, documentar `NO-GO real`, y hacer que tests esperen `orders_submitted=false`.

- [ ] **Step 3: Si Opcion B**

Conectar `build_alpaca_live_client`, `AlpacaLiveBroker(submit_enabled=True)`, risk config live auditada y allowlist. Tests deben usar fake client/fake broker; CI nunca debe leer credenciales reales.

- [ ] **Step 4: Verificar**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_run_live_canary tests.test_live_alpaca_connection tests.test_live_alpaca_execution -v
bash -n scripts/run-live-canary.sh
python3 scripts/verify-safety-patterns.py --mode live
```

Expected: estado documental y comportamiento coinciden.

### Task 4: Agregar market-open verificable para live

**Files:**
- Modify: `src/trading_ai/execution/live_canary.py`
- Modify: `src/trading_ai/cli.py`
- Test: `tests/test_run_live_canary.py`

- [ ] **Step 1: Escribir tests**

Agregar pruebas que bloqueen submit si:

```text
calendar says non-trading day
broker clock says closed
human confirmation says open but machine gate says closed
```

- [ ] **Step 2: Implementar gate**

Usar calendario NYSE local como minimo y dejar una interfaz inyectable `market_clock` para tests. La confirmacion humana debe ser requisito adicional, no sustituto del gate verificable.

- [ ] **Step 3: Verificar**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_run_live_canary -v
python3 scripts/verify-safety-patterns.py --mode live
```

Expected: mercado cerrado bloquea antes de construir cliente o enviar orden.

### Task 5: Agregar price sanity live

**Files:**
- Modify: `src/trading_ai/execution/live_alpaca.py`
- Modify: `src/trading_ai/execution/live_canary.py`
- Test: `tests/test_live_alpaca_execution.py`
- Test: `tests/test_run_live_canary.py`

- [ ] **Step 1: Escribir tests**

Agregar pruebas para `reference_price` faltante, `live_price` faltante y desviacion superior a `max_price_deviation_pct`.

- [ ] **Step 2: Extender `LiveOrder`**

Agregar campos:

```python
reference_price: float | None = None
live_price: float | None = None
max_price_deviation_pct: float = 0.05
```

- [ ] **Step 3: Validar en `AlpacaLiveBroker.validate_order`**

Para `buy`, bloquear si falta referencia/precio live o si `abs(live_price - reference_price) / reference_price > max_price_deviation_pct`.

- [ ] **Step 4: Verificar**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_alpaca_execution tests.test_run_live_canary -v
python3 scripts/verify-safety-patterns.py --mode live
```

Expected: price sanity bloquea antes de submit.

### Task 6: Reconciliar documentacion post-fixes

**Files:**
- Modify: `docs/live-transition-sprints.md`
- Create or modify: `docs/threat-model-2026-06-26-prelive.md`
- Modify: `docs/paper-real-runbook.md`

- [ ] **Step 1: Actualizar estado S12/S13**

El documento debe declarar exactamente si S12 es `dry-run/fake-only` o `real-submit-ready`.

- [ ] **Step 2: Actualizar threat model**

Cerrar hallazgos historicos mitigados y abrir los nuevos hasta que se corrijan: rollback CLI, broker injection, coverage false-positive, market-open y price sanity live.

- [ ] **Step 3: Verificar**

Run:

```bash
python3 scripts/verify-safety-patterns.py --mode live
rg "live-safe-flatten|enable-real-submit|coverage gate|READY_FOR_LIVE_CANARY" docs src tests scripts -S
git diff --check
```

Expected: docs no contradicen el codigo y scanner live sigue limpio.

## Final Verification

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_readiness tests.test_config_loading tests.test_live_execute_session tests.test_run_live_canary tests.test_live_stage_policy tests.test_live_safe_flatten tests.test_live_alpaca_execution tests.test_paper_gate_scripts -v
bash scripts/verify-release.sh
python3 scripts/verify-safety-patterns.py --mode live
python3 scripts/verify-safety-patterns.py --mode futures
git diff --check
```

Expected: all green. Solo despues de esto se puede reabrir decision humana para canary real USD 1.
