# Auditoria Pre-Live Integral - 2026-06-26

**Repo:** `/home/adquiod/Documentos/Algoritmic-IA`  
**Branch auditada:** `live-transition-sprints-impl`  
**Commit auditado:** `a49f257`  
**Decision:** `NO-GO` para canary real USD 1; `GO` solo para dry-run/rehearsal.  

## Resumen Ejecutivo

La postura paper/dry-run esta fuerte: scanners live/futures limpios, release gate verde, readiness conserva `live_trading_authorized=false`, breaker live falla cerrado, y los artefactos live preoperativos declaran `orders_submitted=false`.

No debe ejecutarse dinero real todavia. La revision encontro tres bloqueantes antes de un canary real: el rollback command live apunta a un subcomando inexistente, el wrapper `run-live-canary.sh` no puede construir/injectar un broker live real, y el coverage gate del release puede dar un verde falso porque el shim `pytest.py` intercepta `python -m pytest --cov...` y delega a `unittest`.

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
| `PYTHONPATH=src python3 -m trading_ai.cli live-safe-flatten --dry-run` | FAIL esperado para auditoria: subcomando inexistente |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m pytest --version` | Ejecuta la suite en vez de imprimir version; confirma shadowing por `pytest.py` |

## Hallazgos Priorizados

### BLOCKER-1 - El rollback command live no existe en el CLI

**Evidencia:** `src/trading_ai/execution/live_canary.py:18-19` define `ROLLBACK_COMMAND = "python -m trading_ai.cli live-safe-flatten --dry-run"`. `src/trading_ai/cli.py:541-582` registra `live-readiness-report`, `live-execute-session`, `live-canary` y `live-rehearsal`, pero no `live-safe-flatten`. El comando auditado retorna `invalid choice: 'live-safe-flatten'`.

**Impacto:** el canary escribe una instruccion de rollback que el operador no puede ejecutar. Eso rompe el requisito S12 de rollback listo antes y despues del submit.

**Fix requerido:** registrar un subcomando `live-safe-flatten` o cambiar `ROLLBACK_COMMAND` a un comando existente validado. Debe tener tests CLI y `bash scripts/run-live-canary.sh` debe apuntar al mismo comando.

### BLOCKER-2 - El wrapper S12 no puede ejecutar submit real porque no inyecta broker live

**Evidencia:** `scripts/run-live-canary.sh:30-44` siempre llama `live-canary --enable-real-submit`. En `src/trading_ai/cli.py:2276-2293`, `_live_canary` llama `run_live_canary(...)` sin construir `build_alpaca_live_client` ni `AlpacaLiveBroker`. En `src/trading_ai/execution/live_canary.py:117-120`, si `enable_real_submit=True` y `broker is None`, bloquea con `live_broker_not_injected`. El unico test de submit usa fake broker directamente (`tests/test_run_live_canary.py:102-136`), no el wrapper/CLI con broker live.

**Impacto:** el documento dice que S12 esta completado con camino unico USD 1, pero el camino humano real queda bloqueado por diseno actual. Esto es seguro, pero significa `NO-GO` para canary real hasta corregir la contradiccion o reclasificar S12 como dry-run-only.

**Fix requerido:** decidir una de dos rutas: mantener `NO-GO` y documentar que S12 real no esta conectado, o conectar el boundary live con `build_alpaca_live_client`, `AlpacaLiveBroker(submit_enabled=True)` y risk config live auditada, con tests fake-only y sin ejecucion real en CI.

### BLOCKER-3 - El coverage gate puede ser un falso verde

**Evidencia:** `scripts/verify-release.sh:61-62` ejecuta `$PYTHON_BIN -m pytest --cov=src/trading_ai --cov-report=term-missing --cov-fail-under=75 -q`. El archivo raiz `pytest.py:101-108` filtra argumentos `--cov*` y `-q`, y ejecuta `unittest.defaultTestLoader.discover("tests")`. `python3 -m pytest --version` tambien corre la suite en vez de imprimir version.

**Impacto:** `verify-release.sh` reporta `PASS: coverage gate`, pero no mide ni aplica el umbral de cobertura cuando el shim local intercepta `pytest`. Esto degrada la confiabilidad del release gate.

**Fix requerido:** separar compatibilidad de imports pytest de la ejecucion `python -m pytest`, o hacer que el coverage gate use `coverage run -m unittest discover` + `coverage report --fail-under=75`. Agregar test que falle si `python -m pytest --version` no reporta pytest real cuando el gate de coverage esta activo.

### HIGH-1 - Market-open live depende de una confirmacion humana, no de un gate verificable

**Evidencia:** `scripts/run-live-canary.sh:43` pasa `--market-open-confirmed`. `src/trading_ai/cli.py:575` define ese flag como booleano manual y `src/trading_ai/cli.py:2291` lo pasa a `run_live_canary`. `src/trading_ai/execution/live_canary.py:89-90` solo bloquea si ese booleano es falso.

**Impacto:** una confirmacion exacta podria marcar mercado abierto en un feriado, fuera de RTH o durante halt. En paper ya existe `is_trading_day` y price sanity en `AlpacaPaperBroker`, pero live no lo replica.

**Fix recomendado:** antes de submit real, calcular `market_open` desde una fuente verificable: calendario NYSE + reloj de broker live o snapshot read-only. Mantener la confirmacion humana como segundo factor, no como fuente unica de verdad.

### HIGH-2 - El adapter live no tiene price sanity ni referencia de precio

**Evidencia:** `LiveOrder` en `src/trading_ai/execution/live_alpaca.py:11-21` no incluye `reference_price`. `AlpacaLiveBroker.validate_order` en `live_alpaca.py:55-88` valida allowlist, side, notional/quantity y riesgo, pero no precio. El paper path si valida `reference_price` y desviacion (`src/trading_ai/execution/alpaca_paper.py:50`, `:332-338`).

**Impacto:** al conectar el submit real, una market order live podria salir sin verificar precio plausible contra la senal/evidencia. Para USD 1 el impacto financiero es pequeno, pero el control es obligatorio antes de escalar.

**Fix recomendado:** agregar `reference_price`, `live_price`, `max_price_deviation_pct` y blocker `price_sanity_failed` al boundary live antes de cualquier submit real.

### MEDIUM-1 - Documentacion S0-S13 sobredeclara S12 como completado

**Evidencia:** `docs/live-transition-sprints.md:42` declara S12 completado con wrapper humano y camino unico USD 1. BLOCKER-1 y BLOCKER-2 muestran que el rollback CLI no existe y que el wrapper no puede inyectar broker real.

**Impacto:** un operador podria interpretar que el canary real esta listo cuando el codigo solo es apto para dry-run/fake broker.

**Fix recomendado:** actualizar el estado a `S12 dry-run/fake-only completo; submit real pendiente de fix BLOCKER-1/BLOCKER-2`, o implementar los fixes y mantener la declaracion.

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
