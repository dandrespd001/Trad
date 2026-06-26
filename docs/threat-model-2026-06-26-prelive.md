# Threat Model Pre-Live - 2026-06-26

**Scope:** transicion de paper/dry-run a canary live USD 1 en `live-transition-sprints-impl`.
**Decision:** `NO-GO` para submit real; `GO` para dry-run, rehearsal y evidencia fake-only.

## Activos

- Credenciales Alpaca live y paper.
- Readiness hash, breaker state, rollback evidence y command evidence.
- Limites de riesgo live, allowlist de ETFs, sizing canary USD 1.
- Ledger JSONL, reportes de auditoria y artefactos en `reports/tmp`.

## Trust Boundaries

- `live_connection.py`: unico boundary donde se construye cliente Alpaca live con `paper=False`.
- `live_alpaca.py`: valida ordenes live, risk config, allowlist y price sanity antes de cualquier submit.
- `live_canary.py`: orquesta evidencia humana, readiness, breaker, rollback y mercado.
- `scripts/run-live-canary.sh`: wrapper humano actualmente dry-run/fake-only; no pasa `--enable-real-submit`.
- `pytest.py`: shim de compatibilidad para imports; no es el runner del coverage gate.

## Hallazgos Reconciliados

| ID | Estado | Riesgo | Control actual |
|---|---|---|---|
| T1 rollback CLI inexistente | Cerrado | Rollback command no ejecutable | `live-safe-flatten` existe en CLI, usa fixture local, fake broker y `orders_submitted=false`. |
| T2 coverage falso verde | Cerrado | Release gate reportaba coverage sin medirlo | `verify-release.sh` usa `coverage run -m unittest` y fallback `COVERAGE_PYTHON_BIN`; `python -m pytest --version` no ejecuta suite. |
| T3 market-open manual | Cerrado para dry-run; pendiente de clock real broker para submit | Confirmacion humana podia marcar mercado abierto erroneamente | `run_live_canary` bloquea por calendario NYSE local y acepta `market_clock` inyectable. |
| T4 price sanity live ausente | Cerrado en adapter | Market order podia pasar sin referencia/precio live | `LiveOrder` exige `reference_price`, `live_price` y desviacion maxima para buys. |
| T5 broker injection real | Abierto | El wrapper humano no puede ni debe enviar dinero real todavia | S12 queda dry-run/fake-only; `--enable-real-submit` no esta en `scripts/run-live-canary.sh`. |

## Abuse Paths

- Operador intenta correr canary real desde wrapper: bloqueado porque el wrapper no pasa `--enable-real-submit`.
- Documento stale induce a creer que S12 esta listo para dinero real: mitigado por `docs/live-transition-sprints.md` y este threat model.
- Coverage gate usa shim local en vez de coverage real: mitigado por `COVERAGE_PYTHON_BIN` y test de script.
- Rollback no prevalidado antes de canary: mitigado por `live-safe-flatten` CLI y `rollback_not_prevalidated`.
- Compra live con precio anomalo: bloqueada por `price_sanity_failed` en `AlpacaLiveBroker.validate_order`.

## Residual Risk

El unico bloqueo de diseno para dinero real es conectar broker injection real bajo S12. Esa conexion no debe hacerse hasta agregar:

- snapshot/clock read-only de broker live en el wrapper humano;
- precios `reference_price` y `live_price` trazables en evidencia;
- tests fake-only del CLI real-submit-ready;
- reviewer, reason, readiness hash, breaker limpio y rollback evidence en la misma ejecucion;
- confirmacion humana explicita para reclasificar S12 desde `dry-run/fake-only` a `real-submit-ready`.

Hasta entonces, toda evidencia live debe mantener `orders_submitted=false`.
