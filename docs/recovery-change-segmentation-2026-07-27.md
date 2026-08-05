# Segmentación del working tree para recuperación — 2026-07-27

## Regla de trabajo

El checkout no se debe consolidar con `git add .`. Los cambios se revisan y
preparan por hunks, en este orden, manteniendo separadas las fronteras de
investigación, ejecución, riesgo y despliegue.

El respaldo recuperable previo a cualquier segmentación está en
`.recovery/2026-07-27-0849/`.

## Paquetes y dependencias

### 1. Política de orquestación

- `AGENTS.md`

No se mezcla con código ni documentación del producto.

### 2. Bridge MiniMax

- `skills/delegate-minimax-api/**`
- `tests/test_minimax_api_worker_cli.py`
- `tests/test_minimax_patch_verify_cli.py`
- `docs/minimax-jobs/generic-circular-block-resample.md`

Debe permanecer aislado de `src/trading_ai`, sin entry points ni imports hacia
ejecución, riesgo o configuración. El paquete continúa en `HOLD` hasta que el
worker y el verificador instalados coincidan con `bridge-lock.json`.

### 3. Frontera live NO-GO

- hunks live de `src/trading_ai/execution/live_*.py`
- hunks live de `autonomy_level.py` y `position_sizing.py`
- `src/trading_ai/risk/policy.py`
- `scripts/run-live-canary.sh`
- tests `test_live_*`, `test_run_live_canary.py`,
  `test_autonomy_level.py`, `test_canary_sizing.py` y `test_risk_policy.py`

Criterio: toda ruta mutante permanece deshabilitada incluso con flags
contradictorios.

### 4. P0-01 — datos y snapshots fail-closed

- `src/trading_ai/data/alpaca_market_data.py`
- hunks de snapshots en `execution/alpaca_paper.py`
- hunks de universo/órdenes externas en `execution/sleeve_rebalance.py`
- tests de market data, paper snapshot, retry/idempotencia y rebalance

Depende de la frontera live NO-GO.

### 5. P0-02 — journal, idempotencia y reducción

- `execution/order_journal.py`
- hunks journal/reconcile/flatten de `alpaca_paper.py`
- `paper_safe_flatten.py`
- `paper_close_session.py`
- `paper_statement.py`
- tests de journal, reduce-only, flatten, close y statement

Depende de los contratos de snapshot de P0-01.

### 6. P0-03 — backtest causal

- `backtest/engine.py`
- `backtest/portfolio.py`
- sólo hunks `sleeve-backtest` de `cli.py`
- sólo hunks `next_open_v2` de `sleeve_revalidation.py`
- tests de backtest, señales, portfolio, allocate y revalidation

Es técnicamente independiente de P0-01/P0-02, pero precede cualquier evidencia
económica. La implementación no equivale a edge demostrado.

### 7. P0-04 — costes y ledger económico

- `execution/execution_costs.py`
- `execution/execution_evidence_ledger.py`
- `execution/sleeve_gate1_report.py`
- hunks económicos de `paper_performance.py`, `paper_statement.py` y
  `alpaca_paper.py`
- tests de costes, ledger, fills, Gate 1 y performance

Depende de fills/journal P0-02 y de la semántica de costes P0-03. No declarar
P0-04 cerrado mientras falten quotes, fees, FX, busts y shortfall completo.

### 8. P0-05a — autoridad única, núcleo

- `account_supervisor.py`
- `paper_account_executor.py`
- `paper_executor_authz.py`
- `paper_executor_client.py`
- `paper_executor_daemon.py`
- `paper_executor_ipc.py`
- `paper_executor_journal.py`
- `paper_executor_service.py`
- soporte y tests `paper_executor_*`

Depende del journal durable P0-02.

### 9. P0-05b — migración de consumidores

- hunks IPC de `cli.py` y `cli_paper.py`
- `paper_execute_session.py`
- `paper_monitor.py`
- `paper_position_watch.py`
- `paper_auto_cycle.py`
- `paper_auto_sessions.py`
- `paper_operator_status.py`
- hunks IPC/reduce-only de rebalance y circuit breaker
- `scripts/export-alpaca-paper-statement.py`
- tests equivalentes

No se mezcla con apertura server-side. Las aperturas continúan bloqueadas.

### 10. Deploy/systemd

- `deploy/systemd/**`
- `tests/test_paper_executor_packaging.py`

Depende de P0-05a/P0-05b revisados. Este paquete sólo empaqueta: no instala,
habilita ni inicia servicios.

### 11. Infraestructura estadística P1

- `src/trading_ai/research/block_bootstrap.py`
- `tests/test_research_block_bootstrap.py`

No se mezcla con P0-03 ni se presenta como evidencia de rentabilidad.

### 12. Dependencias y calidad

- `pyproject.toml`, separado
- lockfile reproducible futuro
- shards Ruff/mypy por subsistema
- runner canónico Python 3.12

No usar autofix masivo sobre ejecución o riesgo.

### 13. Documentación

Primero se actualizan los documentos P0 que correspondan a paquetes ya
verificados. README, goal, handover, informes, checklist y runbooks se
consolidan al final. Ninguna afirmación de readiness sobrevive si su gate
aislado no pasó.

## Archivos que requieren separación manual por hunks

- `src/trading_ai/cli.py`: P0-03, P0-04, P0-05 y live.
- `src/trading_ai/cli_paper.py`: operación, riesgo y consumidores IPC.
- `src/trading_ai/config.py`: configuración central y auditoría live.
- `src/trading_ai/execution/alpaca_paper.py`: P0-01, P0-02, P0-04 y broker.
- `src/trading_ai/execution/sleeve_rebalance.py`: P0-01, P0-03, P0-04 y P0-05.
- `src/trading_ai/execution/sleeve_gate1_report.py`: ledger, promoción y
  reconciliación.
- `paper_execute_session.py` y `paper_safe_flatten.py`: IPC y control financiero.
- `src/trading_ai/risk/policy.py`: revisión directa; nunca MiniMax.
- scripts modificados: revisión directa por tocar seguridad, credenciales o
  despliegue.

## Primer criterio antes de preparar commits

Las pruebas que usan rutas por defecto deben ejecutarse en una copia temporal.
El estado operativo del checkout debe conservar hash y mtime. El runner inicial
es `scripts/run-tests-isolated.sh`; todavía falta verificar la suite completa.

