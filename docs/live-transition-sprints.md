# Plan Maestro de Transicion Paper a Live Gobernado

**Fecha de revision:** 2026-06-26
**Broker objetivo:** Alpaca, ETFs US
**Primera orden real objetivo:** USD 1, pendiente de aprobacion despues de cerrar blockers pre-live.
**Principio rector:** ningun submit real hasta que S12 cambie explicitamente de dry-run/fake-only a real-submit-ready; todo artefacto previo debe declarar `orders_submitted: false`.

---

## Estado Verificado del Repositorio

Esta reescritura partio del estado real observado en el repo, no del plan anterior. Tras la implementacion incremental de S0-S13, el estado operativo verificado queda asi:

| Area | Estado actual | Evidencia |
|---|---|---|
| Boundary paper | Existe `AlpacaPaperBroker`; el cliente Alpaca paper se construye con `paper=True`. | `src/trading_ai/execution/alpaca_paper.py`, `src/trading_ai/execution/alpaca_connection.py` |
| Readiness live | `live_readiness` puede producir `READY_FOR_LIVE_CANARY`, pero conserva `live_trading_authorized: false` y `orders_submitted: false`. Es evidencia, no autorizacion. | `src/trading_ai/execution/live_readiness.py`, `tests/test_live_readiness.py` |
| Scanner live | El scanner rechaza claves de autorizacion live puestas en modo enabled dentro de `src`, `configs`, `scripts`, `docs`, `README.md` y `.github`. El documento anterior rompia este gate. | `scripts/verify-safety-patterns.py` |
| CI/tests | El release gate local corre sin red ni secretos y mantiene compatibilidad para tests que importan `pytest` en entornos sin pytest instalado. | `scripts/verify-release.sh`, `pytest.py` |
| Config/risk | `load_risk_config` exige `allow_live` como keyword explicito; los callers paper pasan `allow_live=False` y el uso excepcional queda auditado. | `src/trading_ai/config.py`, `tests/test_config_loading.py`, `rg "load_risk_config\\("` |
| Graduacion paper | `PAPER_STAGES` solo contiene `CANARY`, `SCALE_UP`, `READINESS`; no debe incluir etapas live. | `src/trading_ai/config.py`, `src/trading_ai/execution/paper_graduation.py` |
| Sizing | Existe sizing con trazabilidad para canary y bloqueo por edge neto no positivo; USD 1 queda como primer notional live. | `src/trading_ai/execution/position_sizing.py`, `tests/test_canary_sizing.py` |
| Live adapter | Existen boundaries live aislados. `paper=False` queda en `live_connection.py`; el wrapper humano S12 queda dry-run/fake-only hasta cerrar market-open verificable, price sanity live y broker injection gobernado. | `src/trading_ai/execution/live_connection.py`, `src/trading_ai/execution/live_alpaca.py`, `src/trading_ai/execution/live_canary.py`, `scripts/run-live-canary.sh` |
| Operacion | Hay dry-run live, breaker, reconciliacion, rollback, observabilidad, rehearsal, canary USD 1 y politica de escala USD 50-100 separada; el release gate local esta verde. | `src/trading_ai/execution/live_execute_session.py`, `src/trading_ai/execution/live_stage_policy.py`, `tests/test_live_rehearsal.py`, `scripts/verify-release.sh` |

## Estado de Implementacion S0-S13

| Sprint | Estado | Artefactos principales |
|---|---|---|
| S0 | Completado | Documento maestro reescrito y scanner live limpio. |
| S1 | Completado | `scripts/verify-release.sh` pasa localmente; `pytest.py` evita depender de pytest externo para el gate gobernado. |
| S2 | Completado | `allow_live` es explicito y auditable en `load_risk_config`. |
| S3 | Completado | `PAPER_STAGES` permanece limitado a `CANARY`, `SCALE_UP`, `READINESS`. |
| S4 | Completado | Scorecard cuantitativo de elegibilidad live offline. |
| S5 | Completado | Golden set/evals LLM con autoridad operativa nula. |
| S6 | Completado | Sizing canary con unidades, costos, slippage y bloqueo por edge. |
| S7 | Completado | Adapter live aislado y bloqueado por defecto. |
| S8 | Completado | `live_execute_session` dry-run only con evidencia. |
| S9 | Completado | Rollback, breaker fail-closed y reconciliacion live con fake broker. |
| S10 | Completado | Observabilidad/alerting local y redaccion de secretos. |
| S11 | Completado | Rehearsal E2E con escenarios deterministas y fake broker. |
| S12 | Dry-run/fake-only completado | Wrapper humano `scripts/run-live-canary.sh` genera evidencia canary USD 1 sin `--enable-real-submit`; submit real queda pendiente de fixes pre-live. |
| S13 | Completado | Politica separada `LIVE_CANARY`/`LIVE_SCALE_UP` con evidencia live limpia antes de USD 50-100. |

## Decisiones de Seguridad

- `live_readiness` sigue siendo un reporte de evidencia: puede estar `READY_FOR_LIVE_CANARY`, pero no autoriza trading live por si mismo.
- `LIVE_CANARY` no entra en `PAPER_STAGES`; las etapas live viven en una politica separada.
- Progresion de capital: `CANARY paper USD 1 -> SCALE_UP paper gobernado -> READINESS -> LIVE_DRY_RUN -> LIVE_CANARY USD 1 -> LIVE_SCALE_UP USD 50-100`.
- Ningun sprint puede enviar orden real hasta que S12 sea reclasificado como `real-submit-ready`. Los tests live usan fake broker, fake client o dry-run irreversible.
- El adapter live debe evaluar riesgo con semantica live. No debe heredar a ciegas el modo paper ni llamar al submit paper como atajo.
- Todo go-live requiere reviewer, reason, hash de readiness, evidencia de comando, breaker limpio y rollback prevalidado.
- Los documentos, tests y prompts deben mantener limpio `python3 scripts/verify-safety-patterns.py --mode live`.

## Matriz de Agentes por Sprint

Usar roles pequenos. No todos trabajan en todos los sprints; cada prompt declara los roles necesarios.

| Rol | Responsabilidad |
|---|---|
| Auditor lider | Valida alcance, riesgos, contradicciones del plan y criterios de aceptacion. |
| Execution engineer | Broker boundaries, ordenes, idempotencia, rollback, reconciliacion. |
| Quant/model engineer | Sizing, backtest, leakage, slippage, edge, scorecards. |
| LLM evals engineer | Prompts, evals, guardrails, trazabilidad, autoridad nula de LLM. |
| SRE/QA engineer | CI, deploy, observabilidad, alertas, tests, runbooks. |

## Gates Minimos Para Aceptar Este Documento

```bash
python3 scripts/verify-safety-patterns.py --mode live
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_readiness tests.test_config_loading -v
```

---

# S0 - Auditoria Base y Plan Hygiene

## Prompt Para Codex

```text
Contexto:
Repo: /home/adquiod/Documentos/Algoritmic-IA. El proyecto esta en paper trading gobernado y aun no tiene adapter live. El objetivo de este sprint es que la documentacion y los gates describan el estado real sin romper el scanner live.

Agentes:
- Auditor lider: lidera la auditoria y valida contradicciones.
- SRE/QA engineer: ejecuta gates y registra evidencia.

Archivos ancla:
- docs/live-transition-sprints.md
- scripts/verify-safety-patterns.py
- src/trading_ai/execution/live_readiness.py
- tests/test_live_readiness.py
- src/trading_ai/config.py
- src/trading_ai/execution/paper_graduation.py

Objetivo:
Eliminar contradicciones del plan, documentar el estado verificado y asegurar que el propio documento no active el scanner live.

Tareas TDD:
1. Ejecuta `python3 scripts/verify-safety-patterns.py --mode live` y confirma que cualquier fallo viene de documentacion stale o de un cambio real a corregir.
2. Lee los anchors y lista evidencia concreta: readiness no autoriza live, paper stages no incluyen live, no existe live adapter.
3. Reescribe la seccion afectada del plan para que `READY_FOR_LIVE_CANARY` sea evidencia y no autorizacion.
4. Vuelve a ejecutar el scanner live.
5. Ejecuta los tests de readiness y config.

Restricciones de seguridad:
- No crear adapter live.
- No tocar configs de riesgo, permisos ni modelos.
- No introducir strings que el scanner interprete como autorizacion live enabled.
- No cambiar `PAPER_STAGES`.

Criterios de aceptacion:
- El documento declara primera orden real objetivo USD 1, pendiente de reclasificar S12 a `real-submit-ready`.
- El documento elimina toda exigencia de autorizacion live antes del go-live.
- El scanner live sale limpio.
- `tests.test_live_readiness` y `tests.test_config_loading` pasan o reportan un bloqueo reproducible no causado por el documento.

Comandos de verificacion:
python3 scripts/verify-safety-patterns.py --mode live
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_readiness tests.test_config_loading -v
git diff -- docs/live-transition-sprints.md scripts/verify-safety-patterns.py
```

---

# S1 - CI y Entorno de Tests Reproducible

## Prompt Para Codex

```text
Contexto:
El release gate usa `unittest discover`, mantiene un shim para tests que importan `pytest`, y mide coverage real con `coverage run -m unittest`. El objetivo es un gate local minimo, reproducible, sin red y sin secretos, que no dependa de tener extras instalados por accidente.

Agentes:
- SRE/QA engineer: lidera CI y comandos reproducibles.
- Auditor lider: valida que el gate no omite riesgos live.

Archivos ancla:
- pyproject.toml
- scripts/verify-release.sh
- scripts/verify-paper-focused.sh
- scripts/verify-paper-gates.sh
- tests/conftest.py
- tests/test_config_loading.py
- tests/test_live_readiness.py

Objetivo:
Definir y aplicar una estrategia explicita para `pytest`/`unittest`: o se instala como dependencia dev directa, o el release gate no usa pytest en el perfil minimo. Crear un release gate local que pase con dependencias base del repo o que reporte bloqueos claros.

Tareas TDD:
1. Escribe o ajusta un test/script que falle cuando el gate minimo referencia comandos no disponibles en el entorno base.
2. Ejecuta el gate minimo actual y captura el fallo exacto.
3. Implementa una estrategia unica: `verify-release-minimal.sh` usa `unittest` y scanners; `verify-release.sh` usa coverage real sin pasar por el shim `pytest.py`.
4. Asegura que el scanner live siempre corre en ambos perfiles.
5. Documenta comandos exactos en el runbook o en el propio script.

Restricciones de seguridad:
- Sin red en el gate minimo.
- Sin secrets, broker keys ni llamadas a Alpaca.
- No mutar modelos ni reportes aprobados.
- No relajar scanner live.

Criterios de aceptacion:
- Existe un comando de release minimo que pasa localmente sin red.
- Si pytest no esta instalado, el perfil minimo no falla por imports indirectos.
- El perfil completo documenta prerequisitos de dev extras.
- Los errores de ambiente son legibles y accionables.

Comandos de verificacion:
bash scripts/verify-release.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_readiness tests.test_config_loading -v
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S2 - Hardening de Config/Risk Boundary

## Prompt Para Codex

```text
Contexto:
`load_risk_config(path, *, allow_live=False)` tiene un default que permite a futuros callers olvidar que estan tomando una decision de boundary. El repo tiene muchos callers paper que hoy dependen del default.

Agentes:
- Auditor lider: revisa semantica de risk boundary.
- Execution engineer: actualiza callers de ejecucion.
- SRE/QA engineer: mantiene tests y typing.

Archivos ancla:
- src/trading_ai/config.py
- src/trading_ai/risk/policy.py
- src/trading_ai/execution/paper_execute_session.py
- src/trading_ai/execution/paper_session.py
- src/trading_ai/execution/paper_monitor.py
- tests/test_config_loading.py
- tests/test_position_sizing.py

Objetivo:
Hacer `allow_live` obligatorio, explicito y auditable sin habilitar live ni romper callers paper.

Tareas TDD:
1. Agrega tests que esperen `TypeError` al llamar `load_risk_config(path)` sin `allow_live`.
2. Agrega test de regresion para un payload con live flag enabled construido programaticamente; con `allow_live=False` debe fallar.
3. Agrega test para que `allow_live=True` escriba una linea JSONL de auditoria con timestamp, path y caller redacted.
4. Cambia la firma a `load_risk_config(path, *, allow_live: bool)`.
5. Actualiza todos los callers paper con `allow_live=False`.
6. Ejecuta `rg "load_risk_config\\(" src tests scripts` y confirma que cada llamada visible pasa el keyword.

Restricciones de seguridad:
- No introducir ningun permiso live enabled en YAML, JSON o markdown escaneado.
- No cambiar `live_readiness`.
- No cambiar `PAPER_STAGES`.
- El audit log debe vivir bajo `reports/tmp/` y no imprimir secretos.

Criterios de aceptacion:
- `allow_live` es keyword-required.
- Todos los callers paper pasan `allow_live=False`.
- El unico uso de `allow_live=True` queda cubierto por test de auditoria y no autoriza ordenes.
- Scanner live limpio.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_config_loading tests.test_position_sizing -v
rg "load_risk_config\\(" src tests scripts
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S3 - Graduacion de Capital Paper

## Prompt Para Codex

```text
Contexto:
La graduacion paper ya distingue `CANARY`, `SCALE_UP` y `READINESS`, con caps pequenos. La progresion live debe ser separada. El objetivo es formalizar evidencia paper sin contaminar `PAPER_STAGES` con etapas live.

Agentes:
- Auditor lider: valida gobernanza.
- Quant/model engineer: valida notional y evidencia.
- SRE/QA engineer: cubre regresiones.

Archivos ancla:
- src/trading_ai/config.py
- src/trading_ai/execution/paper_graduation.py
- tests/test_config_loading.py
- tests/test_paper_graduation.py
- configs/risk.yml

Objetivo:
Formalizar la progresion paper: `CANARY` USD 1, `SCALE_UP` paper gobernado, `READINESS` con evidencia. No agregar etapas live a `PAPER_STAGES`.

Tareas TDD:
1. Agrega tests que fallen si `PAPER_STAGES` contiene cualquier stage live.
2. Agrega tests para que `CANARY` requiera USD 1.
3. Agrega tests para que `SCALE_UP` y `READINESS` exijan reviewer, reason y evidencia paper.
4. Agrega un artefacto de decision paper con hash de campaign/phase cuando aplica.
5. Actualiza documentacion y mensajes de error para que `READINESS` signifique listo para revisar, no listo para enviar orden real.

Restricciones de seguridad:
- No crear stage live dentro de paper config.
- No cambiar el default de `configs/risk.yml` fuera de paper-safe.
- No enviar ordenes ni crear adapter live.

Criterios de aceptacion:
- `PAPER_STAGES == {"CANARY", "SCALE_UP", "READINESS"}`.
- `READINESS` puede alimentar `live_readiness`, pero no habilita submit.
- Los tests de config y graduacion pasan.
- Scanner live limpio.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_config_loading tests.test_paper_graduation -v
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S4 - Auditoria Quant, Data y Modelo

## Prompt Para Codex

```text
Contexto:
Antes de dinero real, la estrategia debe demostrar que no depende de leakage, costos omitidos o resultados sin benchmark. Este sprint produce un scorecard de elegibilidad live; no toca ejecucion.

Agentes:
- Quant/model engineer: lidera auditoria de backtest y modelo.
- Auditor lider: revisa criterios de aprobacion.
- SRE/QA engineer: verifica reproducibilidad.

Archivos ancla:
- src/trading_ai/backtest/engine.py
- src/trading_ai/research/metrics.py
- src/trading_ai/evaluation/model_review_decision.py
- tests/test_data_features_backtest.py
- configs/risk.yml
- reports/ o reports/tmp/ generados por backtests

Objetivo:
Crear un scorecard cuantitativo que mida leakage, costos, slippage, benchmark, drawdown, turnover, estabilidad out-of-sample y failure modes.

Tareas TDD:
1. Escribe tests para rechazar scorecards sin benchmark, costos o ventana out-of-sample.
2. Agrega un modulo `src/trading_ai/research/live_eligibility_scorecard.py`.
3. Incluye campos: data_cutoff, timezone, universe, benchmark, fees_bps, slippage_bps, max_drawdown, turnover, exposure, hit_rate, sharpe, oos_period, leakage_checks, blockers.
4. Crea CLI o funcion invocable que escriba JSON y markdown bajo `reports/tmp/live_eligibility/`.
5. Bloquea elegibilidad cuando edge estimado sea menor o igual a cero despues de costos.

Restricciones de seguridad:
- Analisis solamente.
- Sin secrets ni broker client.
- Ningun output autoriza trading live.

Criterios de aceptacion:
- Scorecard reproducible con inputs fechados.
- Bloquea por ausencia de benchmark, costos, slippage u OOS.
- Reporta assumptions y failure modes.
- Tests unitarios pasan.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_eligibility_scorecard tests.test_data_features_backtest -v
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S5 - LLMOps y Guardrails de Modelos LLM

## Prompt Para Codex

```text
Contexto:
El LLM puede resumir, proponer o revisar, pero no debe ordenar ni mutar estado operativo. Se necesita una suite de evaluacion y trazabilidad para prompts/modelos antes de live.

Agentes:
- LLM evals engineer: lidera evals y guardrails.
- Auditor lider: valida autoridad nula.
- SRE/QA engineer: verifica costo/latencia/logs.

Archivos ancla:
- src/trading_ai/llm/factory.py
- src/trading_ai/llm/local_registry.py
- src/trading_ai/execution/llm_context_pack.py
- src/trading_ai/execution/llm_signal_proposals.py
- tests/test_reports_execution_llm_cli.py
- tests/test_paper_signal_arbitration.py

Objetivo:
Crear golden set, eval suite y versionado de prompts/modelos; demostrar que el LLM no puede enviar ordenes, leer secrets ni mutar estado.

Tareas TDD:
1. Agrega golden set JSONL con casos de prompts benignos, ambiguos y maliciosos bajo `tests/fixtures/llm_evals/`.
2. Agrega tests que fallen si una respuesta LLM produce `orders_submitted` distinto de false o autoridad operativa.
3. Versiona prompts/modelos con hash, provider, model id, fecha y parametros.
4. Emite metricas: pass_rate, blocked_unsafe_rate, p95_latency_ms, estimated_cost_usd, redaction_passed.
5. Agrega CLI `llm-eval-suite` o funcion equivalente para escribir reporte JSON/MD bajo `reports/tmp/llm_evals/`.

Restricciones de seguridad:
- No llamar proveedores remotos en tests unitarios.
- No imprimir secrets.
- LLM authority siempre `none` para ejecucion.
- No mutar configs ni modelos promovidos.

Criterios de aceptacion:
- Golden set corre offline con fake provider.
- Cualquier intento de ordenar o mutar estado queda bloqueado.
- Reporte incluye trazabilidad de prompt/modelo y metricas de costo/latencia.
- Scanner live limpio.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_llm_eval_suite tests.test_reports_execution_llm_cli tests.test_paper_signal_arbitration -v
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S6 - Canary Sizing Defendible

## Prompt Para Codex

```text
Contexto:
La primera orden real sera USD 1. El escalado futuro a USD 50-100 debe depender de una recomendacion con unidades correctas y evidencia de edge neto de costos.

Agentes:
- Quant/model engineer: lidera sizing y unidades.
- Auditor lider: valida criterios de bloqueo.
- SRE/QA engineer: verifica reportes reproducibles.

Archivos ancla:
- src/trading_ai/execution/position_sizing.py
- src/trading_ai/research/metrics.py
- src/trading_ai/execution/paper_performance.py
- tests/test_position_sizing.py
- configs/risk.yml

Objetivo:
Crear un sizing report que explique bankroll, stop pct o ATR, fixed fees si existen, slippage bps, cost bps y edge neto. Si edge neto es menor o igual a cero, bloquear.

Tareas TDD:
1. Agrega dataclass `SizingDecision` con notional_usd, cap_usd, bankroll_usd, stop_loss_pct, slippage_bps, fees_usd, expected_edge_usd, blockers y rationale.
2. Cambia `compute_open_notional` o agrega wrapper compatible para devolver trazabilidad estructurada sin romper callers existentes.
3. Agrega tests para unidades: bps a fraccion, ATR/stop a max loss, fees fijos, slippage, cap por stage.
4. Agrega test de bloqueo cuando edge neto <= 0.
5. Genera `reports/tmp/canary_sizing/<date>/sizing.json` y `.md` con recomendacion: USD 1 live inicial, USD 50-100 solo tras evidencia live limpia.

Restricciones de seguridad:
- No cambiar notional real en configs.
- No permitir submit.
- El reporte es recomendacion, no permiso.

Criterios de aceptacion:
- Reporte explica unidades y supuestos.
- Edge neto no positivo bloquea escalado.
- USD 1 queda como primera orden real objetivo, no autorizada hasta reclasificar S12 a `real-submit-ready`.
- Rango USD 50-100 queda condicionado a S13.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_position_sizing tests.test_canary_sizing -v
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S7 - Live Adapter Aislado, Sin Submit Real

## Prompt Para Codex

```text
Contexto:
No existe adapter live. Este sprint crea boundaries de conexion y broker live aislados, pero sin conectarlos a CLI submit y sin permitir orden real.

Agentes:
- Execution engineer: lidera adapter y boundaries.
- Auditor lider: valida que no hay submit real.
- SRE/QA engineer: fake broker tests.

Archivos ancla:
- src/trading_ai/execution/alpaca_connection.py
- src/trading_ai/execution/alpaca_paper.py
- src/trading_ai/execution/live_readiness.py
- tests/test_alpaca_paper_execution.py

Objetivo:
Crear `live_connection.py` y `live_alpaca.py`. `paper=False` aparece solo en el live connection boundary. El adapter live no debe heredar `mode="paper"` ni reutilizar submit paper sin revalidar riesgo live.

Tareas TDD:
1. Agrega `tests/test_live_alpaca_connection.py` con fake TradingClient que verifica `paper=False` y variables `ALPACA_LIVE_*`.
2. Agrega `tests/test_live_alpaca_execution.py` con fake broker que confirma que todo submit devuelve rejected/dry-run hasta que S12 sea `real-submit-ready`.
3. Implementa `build_alpaca_live_client` sin logging de secrets.
4. Implementa `AlpacaLiveBroker` con metodos de validacion, pero `submit_order` bloqueado por defecto con reason `live_submit_not_enabled`.
5. Agrega scanner o test que confirme que `paper=False` no aparece fuera del boundary live permitido.

Restricciones de seguridad:
- No conectar el adapter a CLI submit.
- No leer credenciales en tests unitarios.
- No ejecutar llamadas reales a Alpaca.
- No tocar `AlpacaPaperBroker` salvo imports inevitables.

Criterios de aceptacion:
- Live client builder aislado.
- Submit live bloqueado por defecto.
- Tests usan fake client.
- Scanner live limpio.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_alpaca_connection tests.test_live_alpaca_execution tests.test_alpaca_paper_execution -v
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S8 - Live Execute Session Dry-Run Only

## Prompt Para Codex

```text
Contexto:
Se necesita una sesion live que ejecute todos los gates sin poder enviar orden real. Debe bloquear si `live_readiness_state` no es `READY_FOR_LIVE_CANARY`.

Agentes:
- Execution engineer: lidera session runner.
- Auditor lider: revisa gates y confirmaciones.
- SRE/QA engineer: cubre fake broker y CLI.

Archivos ancla:
- src/trading_ai/execution/live_readiness.py
- src/trading_ai/execution/paper_execute_session.py
- src/trading_ai/execution/live_alpaca.py
- src/trading_ai/cli.py
- tests/test_live_readiness.py

Objetivo:
Crear `live_execute_session` dry-run only. Debe validar readiness, reviewer, reason, hashes, risk config live-safe, price sanity y fake broker; nunca llama submit real.

Tareas TDD:
1. Agrega tests para bloquear sin readiness, con readiness `BLOCKED`, con readiness hash distinto, sin reviewer, sin reason y con dry-run disabled.
2. Agrega test happy path dry-run: `orders_submitted: false`, `broker_client_built: false` salvo fake explícito, evidencia JSON y markdown.
3. Implementa `run_live_execute_session(..., dry_run=True)` sin opcion publica de submit real.
4. Agrega CLI `live-execute-session` que no acepta flags de submit real en este sprint.
5. El output debe incluir command evidence, readiness hash, risk hash, reviewer, reason y blockers.

Restricciones de seguridad:
- Dry-run obligatorio.
- Fake broker solamente.
- No credenciales live.
- No cambiar readiness para autorizar.

Criterios de aceptacion:
- Todas las rutas de fallo escriben evidencia.
- Happy path dry-run pasa solo con `READY_FOR_LIVE_CANARY`.
- No existe camino de submit real.
- Scanner live limpio.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_execute_session tests.test_live_readiness -v
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S9 - Rollback, Circuit Breakers y Reconciliacion Live

## Prompt Para Codex

```text
Contexto:
Antes de cualquier submit real debe existir rollback live prevalidado, breaker persistente fail-closed y reconciliacion preorden/postfill. Este sprint sigue usando fake broker.

Agentes:
- Execution engineer: rollback y reconciliacion.
- Auditor lider: valida fail-closed.
- SRE/QA engineer: pruebas de corrupcion y persistencia.

Archivos ancla:
- src/trading_ai/execution/alpaca_paper.py
- src/trading_ai/execution/paper_safe_flatten.py
- src/trading_ai/execution/paper_risk_state.py
- src/trading_ai/execution/live_execute_session.py
- tests/test_paper_safe_flatten.py

Objetivo:
Implementar `live_safe_flatten`, `live_reconciliation` y `live_circuit_breaker` con checksum persistente. Nuevas aperturas quedan bloqueadas si el breaker esta tripped, corrupto o ausente.

Tareas TDD:
1. Test de breaker missing/corrupt -> tripped fail-closed.
2. Test de reset solo con reviewer, reason y checksum valido.
3. Test de divergencias: unexpected position, quantity mismatch, pending order, fill timeout, symbol fuera de allowlist.
4. Test de `live_safe_flatten` con fake broker: genera ordenes de cierre simuladas, evidencia y `orders_submitted: false` antes de cualquier submit real futuro.
5. Integra precheck del breaker en `live_execute_session`.

Restricciones de seguridad:
- Fake broker hasta que S12 sea `real-submit-ready`.
- No reset automatico de breaker.
- No nuevas aperturas si hay divergencia.
- No secrets en logs.

Criterios de aceptacion:
- Breaker persistente con checksum y fail-closed.
- Reconciliacion preorden y postfill modelada.
- Rollback live existe como dry-run/fake prevalidado.
- Tests cubren corrupcion, missing state y divergencias.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_circuit_breaker tests.test_live_reconciliation tests.test_live_safe_flatten tests.test_live_execute_session -v
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S10 - Observabilidad, Alerting y Deploy

## Prompt Para Codex

```text
Contexto:
El sistema paper tiene reportes offline, pero live necesita metricas, alertas, contenedores y runbooks operativos. Este sprint no habilita submit.

Agentes:
- SRE/QA engineer: lidera observabilidad y deploy.
- Auditor lider: valida secretos y runbooks.
- Execution engineer: expone eventos de ejecucion.

Archivos ancla:
- src/trading_ai/execution/paper_observability.py
- src/trading_ai/execution/paper_monitor.py
- scripts/verify-release.sh
- Dockerfile o compose files existentes si aparecen
- docs/paper-real-runbook.md

Objetivo:
Agregar metricas JSONL/pluggable, alertas tiered, Docker/compose con secretos runtime, volumen persistente, usuario no-root y runbooks.

Tareas TDD:
1. Agrega tests para redaccion de secrets en eventos y errores.
2. Agrega tests para metricas JSONL: gate_status, readiness_state, breaker_state, order_intent_hash, slippage_bps, latency_ms, alert_tier.
3. Implementa writer pluggable: JSONL local primero; no red por defecto.
4. Agrega Dockerfile/compose con env runtime, volumen para `reports/tmp`, usuario no-root y healthcheck local.
5. Documenta runbook de alertas: INFO/WARN/CRITICAL y acciones concretas.

Restricciones de seguridad:
- No secrets baked en imagen.
- No llamar proveedores externos en tests.
- No habilitar submit real.
- Volumen persistente obligatorio para breaker/evidence.

Criterios de aceptacion:
- Eventos y alertas redacted.
- Compose levanta servicio en modo dry-run/paper-safe.
- Runbook cubre readiness blocked, breaker tripped, fill timeout y rollback.
- Scanner live limpio.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_observability tests.test_secret_redaction -v
docker compose config
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S11 - Rehearsal E2E y Chaos Tests

## Prompt Para Codex

```text
Contexto:
Antes del go-live se necesita un rehearsal deterministico que recorra todos los gates con escenarios controlados y fake broker. El objetivo es demostrar que los bloqueos ocurren donde deben.

Agentes:
- SRE/QA engineer: lidera escenarios E2E.
- Execution engineer: fake broker y session runner.
- Auditor lider: revisa evidencia de bloqueo.

Archivos ancla:
- src/trading_ai/execution/live_execute_session.py
- src/trading_ai/execution/live_alpaca.py
- src/trading_ai/execution/live_circuit_breaker.py
- src/trading_ai/execution/live_reconciliation.py
- tests/test_live_execute_session.py

Objetivo:
Crear escenarios deterministas: happy path dry-run, missing confirmation, readiness blocked, breaker tripped, market closed, price sanity fail, fill timeout y rollback.

Tareas TDD:
1. Crea fixtures JSON para cada escenario bajo `tests/fixtures/live_rehearsal/`.
2. Agrega test parametrizado con expected gate, expected blocker y expected evidence fields.
3. Implementa `live_rehearsal` CLI que genera un resumen consolidado con pass/fail por escenario.
4. Asegura que el happy path sigue dry-run y que todos los casos adversos bloquean antes de submit.
5. Escribe evidence index con hashes de inputs y outputs.

Restricciones de seguridad:
- Fake broker solamente.
- No network.
- No credentials.
- No submit real.

Criterios de aceptacion:
- Todos los escenarios tienen evidencia JSON y markdown.
- Cada bloqueo ocurre en el gate esperado.
- Rehearsal completo debe estar verde antes de cualquier reclasificacion real de S12.
- Scanner live limpio.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_rehearsal tests.test_live_execute_session -v
python3 -m trading_ai.cli live-rehearsal --fixtures tests/fixtures/live_rehearsal --output reports/tmp/live_rehearsal/latest
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S12 - Go-Live USD 1 y Operacion Inicial

## Prompt Para Codex

```text
Contexto:
Este sprint esta actualmente en modo dry-run/fake-only. Puede preparar el canary USD 1 y su evidencia, pero no debe conectar submit real hasta que los blockers pre-live queden cerrados: rollback CLI validado, coverage gate real, market-open verificable, price sanity live y broker injection gobernado. No se ejecuta en CI ni por defecto. Requiere evidencia verde de S0-S11, confirmacion humana exacta, readiness hash, breaker limpio y rollback prevalidado.

Agentes:
- Auditor lider: autoriza alcance del cambio y checklist.
- Execution engineer: conecta submit unico y rollback.
- SRE/QA engineer: valida runbook, evidence y alertas.
- Quant/model engineer: confirma sizing USD 1 y edge no bloqueado.

Archivos ancla:
- src/trading_ai/execution/live_execute_session.py
- src/trading_ai/execution/live_alpaca.py
- src/trading_ai/execution/live_safe_flatten.py
- scripts/run-live-canary.sh
- docs/paper-real-runbook.md

Objetivo:
Crear wrapper humano `scripts/run-live-canary.sh` en modo dry-run/fake-only para preparar un canary USD 1, con max one order simulado, max USD 1, rollback listo y evidencia post-check sin submit real.

Tareas TDD:
1. Agrega tests de script/runner: falla si falta evidence S0-S11, falta confirmacion exacta, readiness hash no coincide, breaker tripped, market closed, notional distinto de USD 1, o rollback no prevalidado.
2. Implementa confirmacion exacta: el operador debe escribir una frase que incluya fecha, simbolo, USD 1, reviewer y reason.
3. Mantiene el wrapper sin `--enable-real-submit`; cualquier submit real queda pendiente de una reclasificacion explicita a `real-submit-ready`.
4. Despues del dry-run, ejecutar post-check: order id nulo, fill status nulo, posicion nula, slippage nulo, breaker state, alert tier.
5. Preparar rollback inmediato: `live_safe_flatten` debe estar validado antes de cualquier submit futuro y disponible despues.

Restricciones de seguridad:
- Max una orden.
- Max USD 1.
- Sin escalado en este sprint.
- No retries agresivos; idempotencia por client_order_id y evidence hash.
- Si cualquier precheck falla, no se construye cliente live.

Criterios de aceptacion:
- Wrapper humano con prechecks y confirmacion exacta.
- Wrapper S12 no incluye `--enable-real-submit` mientras el estado sea dry-run/fake-only.
- Evidence incluye command, hashes, reviewer, reason, order id, post-check y rollback command.
- Cualquier fallo bloquea nuevas aperturas.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_run_live_canary tests.test_live_execute_session tests.test_live_safe_flatten -v
bash -n scripts/run-live-canary.sh
python3 scripts/verify-safety-patterns.py --mode live
```

---

# S13 - Escalado a USD 50-100 y Mantenimiento

## Prompt Para Codex

```text
Contexto:
El escalado posterior solo puede ocurrir tras N sesiones live limpias de USD 1. El objetivo es convertir evidencia live real en una politica de mantenimiento y escala limitada a USD 50-100.

Agentes:
- Auditor lider: valida umbrales y aprobaciones.
- Quant/model engineer: recalibra slippage, fills y drift.
- LLM evals engineer: revisa drift de prompts/modelos.
- SRE/QA engineer: mantiene release gate, secretos y on-call.
- Execution engineer: valida idempotencia y rollback en escala.

Archivos ancla:
- reports/tmp/live_canary/
- src/trading_ai/execution/live_execute_session.py
- src/trading_ai/execution/live_reconciliation.py
- src/trading_ai/research/live_eligibility_scorecard.py
- src/trading_ai/research/canary_sizing.py
- docs/paper-real-runbook.md

Objetivo:
Permitir `LIVE_SCALE_UP` separado de paper stages, solo si hay N sesiones live limpias, slippage/fills calibrados, LLM/model drift revisado, secretos rotados y release gate verde.

Tareas TDD:
1. Agrega policy separada `live_stage_policy.py` con stages live que no tocan `PAPER_STAGES`.
2. Tests: bloquea escala si sesiones limpias < N, si slippage real excede threshold, si hubo rollback, si drift LLM/model excede limite, si secrets no se rotaron, o si release gate falla.
3. Genera scorecard de escala con evidencia de fills, slippage, latencia, breaker resets, alertas, drawdown y edge neto.
4. Actualiza runbook on-call con rotacion de secretos, rollback, postmortem y cadencia de mantenimiento.
5. Mantiene USD 50-100 como rango maximo inicial de escala, no como default.

Restricciones de seguridad:
- No subir notional sin aprobacion humana documentada.
- No mezclar live stages con paper stages.
- No relajar max one order per session hasta que la politica lo pruebe.
- No ocultar rollbacks ni breaker trips.

Criterios de aceptacion:
- Politica live separada y testeada.
- Scorecard usa datos live reales de USD 1.
- USD 50-100 requiere evidencia limpia y aprobacion humana.
- Release gate y scanner live verdes.

Comandos de verificacion:
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_stage_policy tests.test_canary_sizing tests.test_live_reconciliation -v
bash scripts/verify-release.sh
python3 scripts/verify-safety-patterns.py --mode live
```

---

## Criterios Globales de No-Regresion

- Antes de que S12 sea `real-submit-ready`, todos los artefactos live preoperativos deben incluir `orders_submitted: false`.
- `live_readiness_state == READY_FOR_LIVE_CANARY` solo habilita revision humana y dry-run live; no habilita submit por si solo.
- Cualquier modulo que lea secrets debe tener tests de redaccion.
- Todo submit real debe tener `client_order_id` idempotente, hash de evidence, reviewer y reason.
- Cualquier divergencia de posicion, fill timeout, market closed o price sanity fail bloquea nuevas aperturas.
- Cada sprint debe cerrar con scanner live limpio.

## Comandos Base de Auditoria Continua

```bash
python3 scripts/verify-safety-patterns.py --mode live
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_live_readiness tests.test_config_loading -v
rg "PAPER_STAGES|READY_FOR_LIVE_CANARY|paper=False|orders_submitted|load_risk_config\\(" src tests scripts docs -S
```
