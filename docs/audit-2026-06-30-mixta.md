# Auditoria Mixta del Proyecto - 2026-06-30

**Repo:** `/home/adquiod/Documentos/Algoritmic-IA`
**Rama auditada:** `live-transition-sprints-impl`
**Commit base auditado:** `dfe2d73`
**Estado auditado:** arbol con cambios locales sin commit sobre live canary real-submit-ready y dataset LLM.
**Decision:** `GO` para dry-run/rehearsal y paper gates; `CONDITIONAL GO` para canary live USD 1 solo con decision humana posterior, credenciales live de proceso, risk config runtime no versionado, doble confirmacion exacta, clock/precio broker live y rollback evidence; `NO-GO` para escala USD 50-100 hasta cerrar riesgo residual y contar evidencia live USD 1 limpia.

## Resumen Ejecutivo

La postura general del proyecto es fuerte para paper/dry-run: el gate minimo y el gate completo pasan, el scanner live/futures esta limpio, el coverage real queda en 86%, y las rutas LLM siguen con `llm_authority=none`, redaccion y casos adversariales.

El cambio actual reclasifica S12 como `real-submit-ready, not executed`: ya puede construir runtime Alpaca live y enviar una unica orden USD 1 si el operador activa `ENABLE_REAL_SUBMIT=YES_I_UNDERSTAND_LIVE_ORDER` y entrega la segunda confirmacion exacta. No se ejecuto ningun submit real durante esta auditoria.

No encontre blockers que impidan aceptar el estado dry-run/rehearsal. Si el objetivo inmediato es ejecutar dinero real, hay un hallazgo de diseno que conviene cerrar antes: `run_live_canary()` permite que callers directos inyecten un broker con `enable_real_submit=True` sin exigir `risk_limits` ni `allowlist` en el nucleo. El CLI y wrapper actuales si cargan esos controles, pero el boundary seguro deberia vivir tambien dentro del orquestador.

## Evidencia Ejecutada

| Comando | Resultado |
|---|---|
| `git status --short --branch` | Rama alineada con `origin/live-transition-sprints-impl`; 11 archivos modificados y `.coverage` no versionado. |
| `git diff --stat` | 821 inserciones, 42 borrados; foco en live canary/runtime, LLM factory, tests y docs. |
| `git diff --check` | PASS. |
| `python3 scripts/verify-safety-patterns.py --mode live` | PASS. |
| `python3 scripts/verify-safety-patterns.py --mode futures` | PASS. |
| `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv312/bin/python -m unittest tests.test_run_live_canary tests.test_live_alpaca_connection tests.test_live_alpaca_execution tests.test_live_stage_policy tests.test_live_readiness tests.test_secret_redaction tests.test_llm_training_dataset -v` | PASS, 40 tests. |
| `./scripts/verify-release-minimal.sh` | PASS; entorno core, 77 tests focales, 50 tests minimos, diff/model/scanners. |
| `./scripts/verify-release.sh` | PASS; focused paper tests 468, full suite 796 en quiet y 796 en verbose, artifact policy, coverage 86%, mypy scoped, pip-audit dry-run, bandit. |

## Hallazgos Priorizados

### HIGH-1 - El orquestador live permite bypass de risk/allowlist si se llama fuera del CLI

**Evidencia:** `run_live_canary()` solo agrega `live_trading_not_allowed_by_risk_config` si `risk_limits is not None` y solo valida allowlist si `allowlist is not None` (`src/trading_ai/execution/live_canary.py:115-125`). Luego, si no hay blockers y `enable_real_submit=True`, envia la orden al `broker` inyectado (`src/trading_ai/execution/live_canary.py:222-236`). El CLI si exige `--risk-live`, carga `load_risk_config(..., allow_live=True)` y pasa `allowlist` desde `configs/universe.yml` (`src/trading_ai/cli.py:2350-2395`), pero esa proteccion no esta duplicada en el nucleo.

**Impacto:** el camino oficial wrapper/CLI esta gobernado, pero un caller directo o una refactorizacion futura podria activar submit real con un broker inyectado y sin risk runtime/allowlist en el orquestador. En un sistema de trading, el fail-closed debe estar en la funcion que decide submit, no solo en el adapter o CLI.

**Recomendacion:** si `enable_real_submit=True`, bloquear cuando `risk_limits is None` o `allowlist is None` antes de construir/runtime/submission, con tests que demuestren que un fake broker no recibe orden sin esos controles. Mantener tests de CLI como cobertura adicional.

### MEDIUM-1 - Fallos de runtime live quedan demasiado opacos para operacion

**Evidencia:** `run_live_canary()` captura cualquier excepcion de `runtime_factory()` y registra solo `live_runtime_build_failed` (`src/trading_ai/execution/live_canary.py:158-164`). `AlpacaLiveRuntime.live_price()` tambien captura cualquier excepcion y devuelve `None` (`src/trading_ai/execution/live_connection.py:38-44`).

**Impacto:** la conducta es fail-closed, lo cual es correcto, pero la evidencia operacional no distingue falta de dependencia, credenciales ausentes, fallo de clock, fallo de market data o error transitorio. Eso complica una corrida humana de USD 1 y puede inducir reintentos manuales poco claros.

**Recomendacion:** conservar redaccion de secretos, pero registrar un `runtime_error_code` no sensible: `missing_live_credentials`, `alpaca_dependency_missing`, `market_clock_error`, `market_data_unavailable`, `live_runtime_unexpected_error`.

### LOW-1 - El filtro LLM `llm_*` puede excluir fuentes validas por nombre de directorio

**Evidencia:** `_build_examples()` salta cualquier JSON cuyo path tenga cualquier componente que empiece por `llm_` (`src/trading_ai/llm/factory.py:631-633`, `src/trading_ai/llm/factory.py:669-670`). El test cubre el caso intencional de no reingerir `llm_local_training` (`tests/test_llm_training_dataset.py:28-50`, `tests/test_llm_training_dataset.py:104-113`).

**Impacto:** el control evita contaminacion recursiva de datasets generados, pero podria excluir evidencia legitima si un `source-root` externo incluye carpetas operativas con prefijo `llm_`.

**Recomendacion:** documentar el convenio o restringir el filtro a directorios generados conocidos, por ejemplo `llm_local_training`, `llm_training_dataset`, `llm_eval`, `llm_candidate_report`.

## Controles Fuertes Verificados

- El wrapper humano conserva dry-run por defecto y solo agrega `--enable-real-submit` dentro del gate exacto `ENABLE_REAL_SUBMIT=YES_I_UNDERSTAND_LIVE_ORDER` con `RISK_LIVE`, `REFERENCE_PRICE` y `CONFIRM_LIVE_SUBMIT` (`scripts/run-live-canary.sh:47-65`).
- El CLI exige `--risk-live`, `--reference-price` y `--confirm-real-submit` antes de artefactos/runtime live (`src/trading_ai/cli.py:2350-2359`; tests en `tests/test_run_live_canary.py:40-74` y `tests/test_run_live_canary.py:76-119`).
- El runtime live es el unico boundary con `paper=False`, cubierto por test de confinamiento (`src/trading_ai/execution/live_connection.py:61-76`; `tests/test_live_alpaca_connection.py:78-85`).
- El canary no construye runtime live si hay blockers offline previos, y los tests lo fijan (`src/trading_ai/execution/live_canary.py:158-164`; `tests/test_run_live_canary.py:76-119`).
- Price sanity y clock broker bloquean antes de submit real (`src/trading_ai/execution/live_canary.py:174-203`; tests en `tests/test_run_live_canary.py:180-240`).
- `configs/risk.yml` versionado conserva `live_trading_allowed: false` y limita `max_price_deviation_pct` a `0.05` (`configs/risk.yml:1-22`).
- El dataset LLM evita reingestion de artefactos generados, agrega holdouts adversariales y mantiene redaccion/autoridad nula (`src/trading_ai/llm/factory.py:631-734`; `tests/test_llm_training_dataset.py:91-113`).
- El threat model y plan maestro ya declaran S12 como `real-submit-ready, not executed` y separan readiness de autorizacion live (`docs/threat-model-2026-06-26-prelive.md:3-19`, `docs/live-transition-sprints.md:45-53`).

## Decision Go/No-Go

- **Paper, dry-run, rehearsal y evidencia:** `GO`. Los gates pasan, no se leen secretos y los controles mantienen `orders_submitted=false` salvo tests fake broker.
- **Canary live USD 1:** `CONDITIONAL GO`, no ejecutado. Requiere decision humana posterior, proceso con credenciales live, `RISK_LIVE` no versionado con live permitido, `REFERENCE_PRICE` trazable, doble confirmacion exacta, clock/precio live broker y rollback evidence. Antes de ejecutar, recomiendo cerrar `HIGH-1`.
- **Escala USD 50-100:** `NO-GO`. Requiere sesiones USD 1 reales limpias, scorecard live, reconciliacion/fill/rollback observados, y cierre de los hallazgos de esta auditoria.

## Riesgo Residual

El riesgo principal ya no es un blocker tecnico de wiring, sino error humano y bypass por uso no oficial del API interno. El camino wrapper/CLI esta bien acotado, pero el nucleo `run_live_canary()` deberia fallar cerrado incluso si se llama directamente. Hasta que se cierre ese punto, cualquier ejecucion real debe limitarse estrictamente al wrapper/CLI auditado y conservar la evidencia completa de runtime, precio, clock, order id, fill status y rollback.
