# Auditoria Pre-Live Integral - 2026-06-26

**Repo:** `/home/adquiod/Documentos/Algoritmic-IA`  
**Branch auditada:** `live-transition-sprints-impl`  
**Commit auditado:** `a49f257`  
**Decision:** `NO-GO` para canary real USD 1; `GO` solo para dry-run/rehearsal.  
**Actualizacion post-fixes:** `BLOCKER-1`, `BLOCKER-3`, `HIGH-1`, `HIGH-2` y `MEDIUM-1` quedaron cerrados en codigo/docs. El bloqueo residual para dinero real es broker injection real bajo S12, mantenido intencionalmente como `dry-run/fake-only`. Ver `docs/threat-model-2026-06-26-prelive.md`.

## Resumen Ejecutivo

La postura paper/dry-run esta fuerte: scanners live/futures limpios, release gate verde, readiness conserva `live_trading_authorized=false`, breaker live falla cerrado, y los artefactos live preoperativos declaran `orders_submitted=false`.

No debe ejecutarse dinero real todavia. La revision inicial encontro tres bloqueantes antes de un canary real: rollback CLI inexistente, wrapper sin broker injection real y coverage gate con posible verde falso. Tras los fixes, rollback CLI y coverage real quedaron cerrados; S12 sigue sin broker injection real por decision de seguridad y el wrapper ya no pasa `--enable-real-submit`.

## Evidencia Ejecutada

| Comando | Resultado |
|---|---|
| `git status --short --branch` | Rama alineada con remoto; solo `.coverage` local no versionado. |
| `git rev-parse --short HEAD` | `a49f257` |
| `python3 scripts/verify-safety-patterns.py --mode live` | PASS |
| `python3 scripts/verify-safety-patterns.py --mode futures` | PASS |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_readiness tests.test_config_loading tests.test_live_execute_session tests.test_run_live_canary tests.test_live_stage_policy -v` | PASS, 37 tests |
| `bash scripts/verify-release-minimal.sh` | PASS |
| `bash scripts/verify-release.sh` | PASS, 779 tests, ruff, mypy, pip-audit dry-run, bandit |
| `PYTHONPATH=src python3 -m trading_ai.cli live-safe-flatten --dry-run` | FAIL inicial esperado para auditoria: subcomando inexistente. Cerrado posteriormente con CLI `live-safe-flatten` usando fixture local. |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m pytest --version` | Ejecuta la suite en vez de imprimir version; confirma shadowing por `pytest.py` |

## Hallazgos Priorizados

### BLOCKER-1 - El rollback command live no existe en el CLI - CERRADO

**Evidencia inicial:** `src/trading_ai/execution/live_canary.py:18-19` definia `ROLLBACK_COMMAND = "python -m trading_ai.cli live-safe-flatten --dry-run"`. `src/trading_ai/cli.py:541-582` no registraba `live-safe-flatten`. El comando auditado retornaba `invalid choice: 'live-safe-flatten'`.

**Impacto:** el canary escribe una instruccion de rollback que el operador no puede ejecutar. Eso rompe el requisito S12 de rollback listo antes y despues del submit.

**Fix aplicado:** `src/trading_ai/cli.py` registra `live-safe-flatten`, lee `--positions-fixture`, usa fake broker/read-only y mantiene `orders_submitted=false`. `ROLLBACK_COMMAND` ahora muestra flags obligatorios seguros.

### BLOCKER-2 - Broker injection real bajo S12 sigue bloqueado - ABIERTO INTENCIONAL

**Evidencia inicial:** `scripts/run-live-canary.sh:30-44` llamaba `live-canary --enable-real-submit`, pero `_live_canary` no construia `build_alpaca_live_client` ni `AlpacaLiveBroker`. Si `enable_real_submit=True` y `broker is None`, `run_live_canary` bloqueaba con `live_broker_not_injected`.

**Impacto:** el camino humano real queda bloqueado por diseno actual. Esto es seguro y ahora esta documentado: S12 queda `dry-run/fake-only` hasta una reclasificacion explicita a `real-submit-ready`.

**Fix pendiente:** conectar el boundary live real solo despues de snapshot/clock broker read-only, price evidence, tests fake-only del CLI real-submit-ready, reviewer/reason/readiness hash y rollback evidence en la misma ejecucion.

### BLOCKER-3 - El coverage gate puede ser un falso verde - CERRADO

**Evidencia:** `scripts/verify-release.sh:61-62` ejecuta `$PYTHON_BIN -m pytest --cov=src/trading_ai --cov-report=term-missing --cov-fail-under=75 -q`. El archivo raiz `pytest.py:101-108` filtra argumentos `--cov*` y `-q`, y ejecuta `unittest.defaultTestLoader.discover("tests")`. `python3 -m pytest --version` tambien corre la suite en vez de imprimir version.

**Impacto:** `verify-release.sh` reporta `PASS: coverage gate`, pero no mide ni aplica el umbral de cobertura cuando el shim local intercepta `pytest`. Esto degrada la confiabilidad del release gate.

**Fix aplicado:** `scripts/verify-release.sh` usa `coverage run -m unittest discover` y `coverage report --fail-under=75`, con `COVERAGE_PYTHON_BIN` para evitar el shim `pytest.py`. `python -m pytest --version` falla con mensaje claro si solo esta el shim.

### HIGH-1 - Market-open live depende de una confirmacion humana, no de un gate verificable - CERRADO PARA DRY-RUN

**Evidencia:** `scripts/run-live-canary.sh:43` pasa `--market-open-confirmed`. `src/trading_ai/cli.py:575` define ese flag como booleano manual y `src/trading_ai/cli.py:2291` lo pasa a `run_live_canary`. `src/trading_ai/execution/live_canary.py:89-90` solo bloquea si ese booleano es falso.

**Impacto:** una confirmacion exacta podria marcar mercado abierto en un feriado, fuera de RTH o durante halt. En paper ya existe `is_trading_day` y price sanity en `AlpacaPaperBroker`, pero live no lo replica.

**Fix aplicado:** `run_live_canary` bloquea por calendario NYSE local y permite inyectar `market_clock`; antes de dinero real debe conectarse un clock/snapshot read-only del broker.

### HIGH-2 - El adapter live no tiene price sanity ni referencia de precio - CERRADO

**Evidencia:** `LiveOrder` en `src/trading_ai/execution/live_alpaca.py:11-21` no incluye `reference_price`. `AlpacaLiveBroker.validate_order` en `live_alpaca.py:55-88` valida allowlist, side, notional/quantity y riesgo, pero no precio. El paper path si valida `reference_price` y desviacion (`src/trading_ai/execution/alpaca_paper.py:50`, `:332-338`).

**Impacto:** al conectar el submit real, una market order live podria salir sin verificar precio plausible contra la senal/evidencia. Para USD 1 el impacto financiero es pequeno, pero el control es obligatorio antes de escalar.

**Fix aplicado:** `LiveOrder` incluye `reference_price`, `live_price` y `max_price_deviation_pct`; `AlpacaLiveBroker.validate_order` bloquea buys con precios faltantes o desviacion superior al limite.

### MEDIUM-1 - Documentacion S0-S13 sobredeclara S12 como completado - CERRADO

**Evidencia:** `docs/live-transition-sprints.md:42` declara S12 completado con wrapper humano y camino unico USD 1. BLOCKER-1 y BLOCKER-2 muestran que el rollback CLI no existe y que el wrapper no puede inyectar broker real.

**Impacto:** un operador podria interpretar que el canary real esta listo cuando el codigo solo es apto para dry-run/fake broker.

**Fix aplicado:** `docs/live-transition-sprints.md` declara S12 como `dry-run/fake-only`; el wrapper no incluye `--enable-real-submit`.

### MEDIUM-2 - Threat model historico quedo stale frente al estado live actual

**Evidencia:** `docs/threat-model-2026-06-24-algoritmic-ia.md` conserva un estado paper-only historico y hallazgos como `allow_live=True` latente ya mitigado por `src/trading_ai/config.py:71-105`. El propio documento advierte que debe reconciliarse con auditorias posteriores.

**Impacto:** bajo riesgo runtime, pero puede confundir decisiones pre-live.

**Fix recomendado:** crear un threat model actualizado post-S13 o agregar una seccion de reconciliacion que apunte a esta auditoria y cierre/actualice T1-T8.

## Controles Fuertes Verificados

- `live_readiness` produce `READY_FOR_LIVE_CANARY` sin autorizar live: `src/trading_ai/execution/live_readiness.py` y tests focales pasan.
- `load_risk_config` exige `allow_live` explicito y audita el uso excepcional: `src/trading_ai/config.py:71-105`.
- Breaker live falla cerrado para missing/corrupt/checksum mismatch: `src/trading_ai/execution/live_circuit_breaker.py:53-69`.
- `paper=False` esta confinado al boundary live: `src/trading_ai/execution/live_connection.py:56`.
- Deploy dry-run define usuario no-root y volumen persistente: `Dockerfile:8-23`, `compose.yml:7-15`.
- Observabilidad live usa JSONL local y redaccion: `src/trading_ai/execution/live_observability.py:13-86`.

## Decision Go/No-Go

- **Dry-run/rehearsal:** `GO`. Los gates pasan y no construyen cliente live.
- **Live canary USD 1:** `NO-GO`. Deben cerrarse BLOCKER-1, BLOCKER-2 y BLOCKER-3 antes de pedir aprobacion humana para dinero real.
- **Escala USD 50-100:** `NO-GO`. Ademas de los blockers, requiere sesiones live USD 1 limpias y price sanity live.
