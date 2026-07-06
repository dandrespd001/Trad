# Threat Model Pre-Live - 2026-06-26

**Scope:** transicion de paper/dry-run a canary live USD 1 en `live-transition-sprints-impl`.
**Decision:** `S12 real-submit-ready, not executed`; `GO` para dry-run, rehearsal y evidencia, y capacidad de submit USD 1 solo bajo decision humana posterior con doble confirmacion exacta.

## Activos

- Credenciales Alpaca live y paper.
- Readiness hash, breaker state, rollback evidence y command evidence.
- Limites de riesgo live, allowlist de ETFs, sizing canary USD 1.
- Ledger JSONL, reportes de auditoria y artefactos en `reports/tmp`.

## Trust Boundaries

- `live_connection.py`: unico boundary donde se construyen cliente Alpaca trading live con `paper=False` y cliente market data live read-only.
- `live_alpaca.py`: valida ordenes live, risk config, allowlist y price sanity antes de cualquier submit.
- `live_canary.py`: orquesta evidencia humana, readiness, breaker, rollback y mercado.
- `scripts/run-live-canary.sh`: wrapper humano dry-run por defecto; solo pasa `--enable-real-submit` con `ENABLE_REAL_SUBMIT=YES_I_UNDERSTAND_LIVE_ORDER`, `RISK_LIVE`, `REFERENCE_PRICE` y segunda confirmacion exacta.
- `pytest_shim.py`: shim de compatibilidad para imports; no vive como `pytest.py` en la raiz ni es el runner del coverage gate.

## Hallazgos Reconciliados

| ID | Estado | Riesgo | Control actual |
|---|---|---|---|
| T1 rollback CLI inexistente | Cerrado | Rollback command no ejecutable | `live-safe-flatten` existe en CLI, usa fixture local, fake broker y `orders_submitted=false`. |
| T2 coverage falso verde | Cerrado | Release gate reportaba coverage sin medirlo | `verify-release.sh` usa `coverage run -m unittest` y fallback `COVERAGE_PYTHON_BIN`; `python -m pytest --version` no ejecuta suite. |
| T3 market-open manual | Cerrado para readiness | Confirmacion humana podia marcar mercado abierto erroneamente | `run_live_canary` bloquea por calendario NYSE local y, en real-submit-ready, exige clock broker live open antes de submit. |
| T4 price sanity live ausente | Cerrado | Market order podia pasar sin referencia/precio live | `LiveOrder` exige `reference_price`, `live_price` y desviacion maxima para buys; CLI obtiene latest trade read-only despues de prechecks offline. |
| T5 broker injection real | Cerrado para readiness | El wrapper no podia construir broker real gobernado | S12 construye runtime Alpaca live solo despues de prechecks offline, doble confirmacion, risk runtime, allowlist, clock, precio y rollback. |

## Abuse Paths

- Operador intenta correr canary real desde wrapper sin opt-in exacto: bloqueado porque el wrapper no pasa `--enable-real-submit` salvo con `ENABLE_REAL_SUBMIT=YES_I_UNDERSTAND_LIVE_ORDER`.
- Documento stale induce a creer que S12 esta listo para dinero real: mitigado por `docs/live-transition-sprints.md` y este threat model.
- Coverage gate usa shim local en vez de coverage real: mitigado por `COVERAGE_PYTHON_BIN` y test de script.
- Rollback no prevalidado antes de canary: mitigado por `live-safe-flatten` CLI y `rollback_not_prevalidated`.
- Compra live con precio anomalo: bloqueada por `price_sanity_failed` en `AlpacaLiveBroker.validate_order`.

## Residual Risk

S12 ya puede construir el runtime live y enviar una unica orden USD 1 si el
operador decide ejecutar posteriormente el comando con credenciales live. El
riesgo residual principal es error humano al suministrar `RISK_LIVE`,
`REFERENCE_PRICE`, readiness hash o confirmaciones. Mitigaciones:

- el wrapper sigue dry-run por defecto;
- CI y tests usan fake broker/client/data y no leen credenciales live;
- si cualquier blocker offline existe, `runtime_factory` no se llama;
- evidencia registra `reference_price`, `live_price`, desviacion, clock broker,
  `broker_client_built`, `credentials_read`, `orders_submitted`, order id,
  fill status y rollback command;
- toda ejecucion real posterior debe limitarse a una orden y USD 1.

Hasta una decision humana posterior de submit, la evidencia esperada mantiene
`orders_submitted=false`.
