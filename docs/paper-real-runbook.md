# Runbook: Alpaca Paper Real Controlado

Este runbook describe el unico flujo permitido para enviar una orden real contra
Alpaca paper desde un paquete `paper-session` aprobado. No habilita live trading,
no descarga datos, no recalcula senales y no modifica el paquete offline
aprobado.

## Flujo diario resumido

Para una corrida manual diaria o un cron paper-only, mantenga esta secuencia:

1. Ejecute `prepare-paper-daily --run-offline-smoke` sobre un paquete aprobado
   o una fuente manual aprobada.
2. Revise `readiness.json`/`readiness.md`: `status=READY`,
   `ready_for_paper_daily=true` y `offline_smoke.exit_code=0` son obligatorios.
3. Solo despues de esa revision, ejecute `paper-daily-from-readiness` con el
   `readiness.json` del paquete y las confirmaciones readiness/broker
   explicitas.
4. Ejecute o revise `paper-monitor` y detenga acciones paper si retorna
   `CRITICAL` o `ERROR`.
5. Genere `paper-campaign-report` para consolidar readiness, sesiones,
   closeouts, decisiones y performance disponible.
6. Ejecute `paper-day-close` para registrar `CONTINUE`, `REVIEW`, `STOP` o
   `ERROR`.
7. Ejecute `paper-statement-validate` si hay statement paper manual local.
8. Ejecute `paper-performance-report`; use `--broker-statement` con el
   statement raw o normalizado solo si esta disponible localmente.
9. Ejecute `paper-ops-check` antes del siguiente submit paper.
10. Ejecute `paper-evidence-index` para ubicar la evidencia del dia.
11. Al cierre de semana, ejecute `paper-weekly-summary --history-weeks 4`.

Ningun paso de este flujo habilita live trading. Por defecto el monitor tampoco
contacta Alpaca, no lee credenciales broker, no envia ordenes, no cancela
ordenes, no descarga datos, no recalcula senales y no cambia modelos. La unica
excepcion es el snapshot broker read-only opt-in documentado abajo, que exige
`--broker-read-only --confirm-paper` y sigue siendo paper-only.

## Checklist diario paper-only

1. Prepare el paquete con `prepare-paper-daily --run-offline-smoke`.
2. Revise `readiness.json` y `readiness.md`; continue solo con `READY`,
   `ready_for_paper_daily=true` y smoke offline aprobado.
3. Ejecute `paper-daily-from-readiness` con las cuatro confirmaciones solo si
   aplica una corrida broker paper-confirmed.
4. Cierre la evidencia de ejecucion hasta obtener `CLOSED`; si queda
   `PENDING`, `UNMATCHED` o `ERROR`, detenga nuevos submits.
5. Genere o revise `paper-monitor` y trate `CRITICAL`/`ERROR` como stop
   operativo.
6. Genere `paper-campaign-report` para consolidar readiness, sesiones,
   closeouts y blockers.
7. Genere `paper-day-close` y aplique la decision:
   `CONTINUE` permite seguir la rutina paper; `REVIEW` requiere revision
   humana antes del siguiente submit; `STOP` detiene nuevos submits hasta
   resolver blockers; `ERROR` invalida el cierre operativo hasta corregir el
   artefacto o JSON fallido.
8. Genere `paper-performance-report` y, si existe statement manual, agregue
   `--broker-statement /ruta/statement.json`.
9. Genere `paper-ops-check` y no avance al siguiente submit si retorna
   `CRITICAL` o `ERROR`.
10. Archive la decision del dia junto con los artefactos bajo `reports/tmp`.
11. Antes de cerrar la rama o el dia paper, ejecute los gates versionados:

```bash
./scripts/verify-paper-focused.sh
./scripts/verify-paper-artifacts.sh
./scripts/verify-paper-gates.sh
```

`scripts/verify-paper-gates.sh` ejecuta los tests paper enfocados, el
`unittest` completo, `git diff --check` y el gate de artefactos. No introduce
`ruff`, `mypy`, `pre-commit` ni dependencias obligatorias nuevas.

## Operador diario paper-only

El primer paso operativo es preparar readiness y probar la config generada sin
broker ni Telegram:

```bash
PYTHONPATH=src python3 -m trading_ai.cli prepare-paper-daily \
  --approved-dir data/raw/approved/core_etfs/1d \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16 \
  --config configs/universe.yml \
  --risk configs/risk.yml \
  --signal-model models/latest_model.json \
  --run-offline-smoke
```

El paquete queda en
`reports/tmp/paper_daily_prepare/<dataset>/<frequency>/<as_of_date>/`. Antes de
cualquier corrida con broker, revise `offline_smoke.requested=true`,
`offline_smoke.ran=true`, `offline_smoke.exit_code=0` y las rutas generadas a
daily, session, observability y monitor. Un smoke con exit `1` bloquea la
corrida broker; un exit `2` es un error operacional. El smoke nunca pasa
`--confirm-paper`, `--confirm-auto-close`, `--confirm-auto-submit` ni
`--send-telegram`.

Para el hito de operacion diaria paper, `paper-daily` sigue siendo el operador
local reusable del runbook. El comando carga `configs/paper_daily.yml` por
defecto, acepta overrides de rutas/fechas y llama directamente a las funciones
locales existentes; no invoca subprocesos.

Sin confirmaciones, solo ejecuta pasos offline, escribe JSON/Markdown y marca
acciones broker como `SKIPPED`. No construye cliente Alpaca y no lee
credenciales broker:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-daily \
  --config configs/paper_daily.yml
```

Para un cron manual paper-only con broker incluido, use la puerta explicita
desde readiness aprobado. Las cuatro confirmaciones son obligatorias:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-daily-from-readiness \
  --readiness reports/tmp/paper_daily_prepare/core_etfs/1d/2026-06-16/readiness.json \
  --confirm-readiness \
  --confirm-paper \
  --confirm-auto-close \
  --confirm-auto-submit
```

`paper-daily-from-readiness` valida antes de construir cualquier cliente
broker: `status=READY`, `ready_for_paper_daily=true`, `exit_code=0`,
`offline_smoke.requested=true`, `offline_smoke.ran=true`,
`offline_smoke.exit_code=0` y `paper_daily_config_path` existente. Si falta una
confirmacion, retorna `2`. Si el readiness o el smoke no estan aprobados,
retorna `1` y escribe un reporte bloqueado. Si el readiness es invalido o la
config no existe, retorna `2`. Cuando llama a `paper-daily`, propaga su salida
`0`/`1`/`2`.

Orden operativo del wrapper:

1. Detecta ejecuciones `SUBMITTED` sin closeout `CLOSED` desde observability,
   incluyendo sesiones diarias anidadas bajo `sessions_root`.
2. Intenta `paper-close-session` sobre esas sesiones solo con
   `--confirm-paper --confirm-auto-close`.
3. Crea o actualiza `paper-session` con datos aprobados.
4. Genera observability y monitor antes del submit.
5. Bloquea un submit nuevo si el monitor esta `CRITICAL`/`ERROR` o queda una
   ejecucion abierta sin closeout cerrado.
6. Ejecuta `paper-execute-session` solo si la sesion nueva esta `READY` y hay
   `--confirm-paper --confirm-auto-submit`.
7. Intenta closeout inmediato de la ejecucion nueva solo con
   `--confirm-auto-close`.
8. Regenera observability y monitor final.

Artefactos por defecto:

- `reports/tmp/paper_daily_prepare/<dataset>/<frequency>/<as_of_date>/paper_daily/broker_confirmed/broker_run.json`
- `reports/tmp/paper_daily_prepare/<dataset>/<frequency>/<as_of_date>/paper_daily/broker_confirmed/broker_run.md`
- `reports/tmp/paper_daily_prepare/<dataset>/<frequency>/<as_of_date>/paper_daily/broker_confirmed/daily.json`
- `reports/tmp/paper_daily_prepare/<dataset>/<frequency>/<as_of_date>/paper_daily/broker_confirmed/daily.md`
- `reports/tmp/paper_daily_prepare/<dataset>/<frequency>/<as_of_date>/paper_daily/broker_confirmed/sessions/daily/<as_of_date>/`
- `reports/tmp/paper_daily_prepare/<dataset>/<frequency>/<as_of_date>/paper_daily/broker_confirmed/observability.json`
- `reports/tmp/paper_daily_prepare/<dataset>/<frequency>/<as_of_date>/paper_daily/broker_confirmed/monitor.json`

El `broker_run.json` incluye `readiness_path`, `paper_daily_config_path`,
`status`, `exit_code`, confirmaciones, rutas broker-confirmed, resumen del
`paper-daily` ejecutado y razones. El JSON diario incluye `schema_version`,
`generated_at`, `as_of_date`, `status`, `run_id`, config redacted, pasos,
acciones broker paper, monitor final, artefactos y razones. `--ledger-output`
agrega un evento redacted `paper_daily`; no guarda cuenta completa, tokens
Telegram, credenciales Alpaca ni respuestas completas de broker.

El wrapper desde readiness fuerza `send_telegram=false`; no expone flags de
Telegram en este hito. En ejecuciones directas de `paper-daily`, Telegram se
delega al monitor final.

Codigos de salida:

- `0`: `paper-daily` termino sin alertas criticas finales ni gate paper
  bloqueado.
- `1`: readiness/smoke no aprobado, monitor final `CRITICAL` o un gate paper
  bloqueo submit/close.
- `2`: confirmacion faltante, readiness/config invalido, monitor `ERROR`,
  dependencia broker faltante, credenciales paper faltantes u otro error
  operacional.

Este operador no habilita live trading, no lee `.env`, no cambia modelos
automaticamente, no descarga datos, no cancela ordenes y no elimina evidencia
previa. El modo broker debe usar `paper-daily-from-readiness`, no una config
editada manualmente sin evidencia.

Antes de ejecutar el modo broker real, confirme que el `readiness.json`
aprobado apunta al `paper_daily.generated.yml` generado por
`prepare-paper-daily --run-offline-smoke` para el `as_of_date` elegido. La
muestra versionada del repositorio sirve para smoke tests y puede quedar
bloqueada por freshness en una corrida diaria real.

## 1. Generar una sesion offline aprobada

Prepare el CSV de mercado fuera del bot, con datos ya aprobados por el operador.
Despues genere el paquete offline:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-session \
  --source-csv /ruta/aprobada/fresh_source.csv \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16 \
  --output-dir reports/tmp/paper_session/latest \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

Opcionalmente, agregue el gate MLflow paper-candidate si ya sincronizo el
registry local a MLflow y registro el alias:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-session \
  --source-csv /ruta/aprobada/fresh_source.csv \
  --from 2026-03-01 \
  --to 2026-06-16 \
  --as-of-date 2026-06-16 \
  --output-dir reports/tmp/paper_session/latest \
  --review-mlflow-paper-candidate \
  --mlflow-registry-dir reports/registry \
  --mlflow-tracking-uri reports/mlruns \
  --mlflow-registered-model-name approved-data-logistic-baseline \
  --mlflow-alias paper-candidate \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

Este gate es paper-only y opt-in. Escribe
`mlflow/paper_candidate_review.json` y `mlflow/paper_candidate_review.md`
dentro de la sesion. Un review `PASSED` agrega evidencia a `session.json` y
`audit/paper_audit.json`; un review `FAILED` bloquea con salida `1`; errores
operativos como MLflow ausente o registry ilegible devuelven salida `2`. El
modelo revisado por MLflow no reemplaza automaticamente el `--signal-model` ni
`models/latest_model.json`.

La salida esperada es:

- `0`: `ready_for_paper_review=true`; el paquete puede pasar a revision manual.
- `1`: freshness, preflight, auditoria o gate MLflow opt-in bloquearon la sesion.
- `2`: error operativo, por ejemplo CSV invalido, ruta faltante o MLflow no disponible.

## 2. Revisar el paquete antes de ejecutar

Revise manualmente estos artefactos del directorio de sesion:

- `session.md`
- `audit/paper_audit.json`
- `paper/paper_signal_order.json`
- `fresh_data/freshness.json`
- `mlflow/paper_candidate_review.json` si uso `--review-mlflow-paper-candidate`

La ejecucion paper real solo debe continuar si `session.json` y
`audit/paper_audit.json` indican `ready_for_paper_review=true`, el audit tiene
`fail_count=0`, freshness esta permitido y el `paper_signal_order.json` contiene
una orden dry-run aceptada para `market buy day`, ETF allowlisted y notional USD
`1.0`. Si uso el gate MLflow, confirme tambien
`summary.mlflow_candidate_review_passed=true`, el alias esperado y que no exista
el finding `mlflow_candidate_review_failed`.

## 3. Preparar entorno paper

Instale la dependencia opcional solo en el entorno que vaya a ejecutar Alpaca
paper:

```bash
python -m pip install -e ".[broker]"
```

Exporte credenciales desde el proceso. No las escriba en `.env` ni en archivos
del repositorio:

```bash
export ALPACA_PAPER_API_KEY="..."
export ALPACA_PAPER_SECRET_KEY="..."
```

Las credenciales se leen solo despues de las confirmaciones CLI y de las
validaciones locales del paquete.

## 4. Ejecutar la orden paper real

Ejecute con ambas confirmaciones explicitas:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-execute-session \
  --session-dir reports/tmp/paper_session/latest \
  --confirm-paper \
  --confirm-submit \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

Opcionales:

- `--output-dir /ruta/evidence`: escribe los artefactos fuera de
  `SESSION_DIR/execution`.
- `--as-of-date YYYY-MM-DD`: fija la fecha de preflight.
- `--max-feature-age-days 5`: controla el limite de edad de features.

La ejecucion no descarga datos, no reentrena modelos, no recalcula senales y no
edita `session.json`, `session.md`, `audit/`, `paper/` ni `fresh_data/`.

La salida esperada es:

- `0`: orden enviada y evidencia escrita.
- `1`: bloqueo de preflight o broker sin envio aceptado.
- `2`: faltan confirmaciones, ruta invalida, JSON invalido o prerrequisito
  operativo faltante.

## 5. Revisar evidencia de ejecucion

Revise los artefactos:

- `execution/paper_execution.json`
- `execution/paper_execution.md`

Si uso `--output-dir`, revise esos mismos nombres dentro del directorio elegido.
Confirme `status`, `order_sent`, `broker_result`, `final_order`, `preflight`,
`open_orders`, `positions` y `account`.

## 6. Cerrar evidencia de sesion

Espere a que Alpaca paper refleje fill y posicion si la orden queda inicialmente
aceptada o pendiente. Luego ejecute el cierre de evidencia:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-close-session \
  --session-dir reports/tmp/paper_session/latest \
  --confirm-paper \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

Opcionales:

- `--execution-report /ruta/paper_execution.json`: usa un reporte de ejecucion
  fuera de `SESSION_DIR/execution`.
- `--output-dir /ruta/closeout`: escribe los artefactos fuera de
  `SESSION_DIR/closeout`.

El comando no envia ordenes, no cancela ordenes, no descarga datos, no
recalcula senales y no autoriza live trading. Valida localmente la sesion,
audit, senal, freshness, reporte de ejecucion y confirmacion antes de leer
credenciales o construir el cliente broker. Despues consulta solo Alpaca paper:
cuenta, posiciones, ordenes abiertas y la orden por `client_order_id`.

Revise:

- `closeout/paper_closeout.json`
- `closeout/paper_closeout.md`

La salida esperada es:

- `0` con `status=CLOSED`: la ejecucion fue `SUBMITTED`, la orden esperada
  coincide con broker, esta `filled` o `partially_filled` y existe la posicion
  esperada.
- `1` con `status=PENDING`: la orden coincide, pero aun falta fill o posicion
  suficiente. Puede rerunear el comando.
- `1` con `status=UNMATCHED`: la orden esta ausente, no coincide en simbolo,
  lado, notional o client id, o broker reporta cancelacion, rechazo o expiracion.
- `2`: error operativo, por ejemplo JSON invalido, ruta faltante, dependencia
  broker ausente o credenciales paper faltantes.

El criterio de aceptacion del primer run operativo es: sesion `READY`,
ejecucion `SUBMITTED`, closeout `CLOSED` y observabilidad sin blockers criticos.
El notional sigue fijo en USD `1.0`.

## 7. Reconciliar, consultar o cancelar

Use el flujo `paper` existente para operar contra Alpaca paper:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper \
  --broker alpaca \
  --real-paper \
  --confirm-paper \
  --reconcile-order \
  --source-report reports/tmp/paper_session/latest/paper/paper_signal_order.json \
  --output reports/tmp/paper/reconciliation.json \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper \
  --broker alpaca \
  --real-paper \
  --confirm-paper \
  --get-order \
  --client-order-id signal-spy-20260616 \
  --output reports/tmp/paper/order_signal_spy.json \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper \
  --broker alpaca \
  --real-paper \
  --confirm-paper \
  --cancel-order \
  --client-order-id signal-spy-20260616 \
  --confirm-cancel \
  --output reports/tmp/paper/cancel_signal_spy.json \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

La cancelacion siempre requiere `--confirm-cancel`. Ningun comando de este
runbook autoriza live trading.

## 8. Consolidar observabilidad offline

El ledger JSONL es paper-only, append-only y opt-in. Si omite
`--ledger-output`, no se escribe ningun evento adicional. Cada linea registra
solo estado, codigos/razones resumidas e identificadores de orden como
`client_order_id`, `symbol`, `side` y `notional`; no guarda secretos, payloads
completos de cuenta ni respuestas completas del broker.

Genere el reporte consolidado sin contactar broker:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-observability \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --output reports/tmp/paper_observability/latest.json \
  --markdown-output reports/tmp/paper_observability/latest.md
```

Tambien puede incluir sesiones concretas con `--session-dir <dir>` repetible.
`paper-observability` lee artefactos existentes (`session.json`,
`audit/paper_audit.json`, `paper/paper_signal_order.json`,
`fresh_data/freshness.json`, `monitoring/drift.json` si existe y
`execution/paper_execution.json` y `closeout/paper_closeout.json` si existen).
No lee credenciales, no descarga datos, no recalcula senales, no envia ordenes
y no autoriza live trading.

## 9. Ejecutar monitor operativo

Ejecute el dashboard paper-only despues de `paper-observability`:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --output reports/tmp/paper_monitor/latest.json \
  --markdown-output reports/tmp/paper_monitor/latest.md \
  --as-of-date 2026-06-16 \
  --min-stable-sessions 60 \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

Tambien puede incluir sesiones concretas con `--session-dir <dir>` repetible.
El monitor reusa la observabilidad offline y escribe:

- `reports/tmp/paper_monitor/latest.json`
- `reports/tmp/paper_monitor/latest.md`

Cada reporte incluye `stability`. La unidad de estabilidad es una sesion paper
completa: `paper_session=READY`, ejecucion `SUBMITTED`, closeout `CLOSED` y sin
diagnostico o blocker asociado. `--min-stable-sessions` usa `60` por defecto.
`stability.status=PASSED` y `ready_for_live_review=true` solo habilitan una
revision manual futura; no autorizan live trading, no cambian `risk.yml` y no
permiten credenciales live.

La salida esperada es:

- `0` con `status=OK`: no hay alertas.
- `0` con `status=WARN`: hay advertencias, por ejemplo ledger solicitado
  faltante, ausencia de sesiones recientes o evidencia incompleta no critica.
- `1` con `status=CRITICAL`: detenga acciones paper hasta resolver los
  bloqueos.
- `2` con `status=ERROR` o error CLI: error operativo, por ejemplo fallo al
  escribir artefactos, fallo del snapshot broker read-only o fallo de Telegram
  cuando `--send-telegram` esta activo.

Alertas criticas:

- diagnosticos de observabilidad por artefactos faltantes o JSON invalido;
- sesiones bloqueadas;
- ejecuciones bloqueadas;
- closeout `PENDING` o `UNMATCHED`;
- ejecucion `SUBMITTED` sin closeout;
- orden broker abierta sin evidencia local de closeout cerrado cuando se usa el
  snapshot broker read-only;
- blockers existentes en observabilidad.

## 10. Consolidar campana paper

Despues de readiness, sesiones, observability y monitor, genere el rollup
read-only de campana:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-campaign-report \
  --sessions-root reports/tmp/paper_session \
  --readiness-root reports/tmp/paper_daily_prepare \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --output reports/tmp/paper_campaign/latest.json \
  --markdown-output reports/tmp/paper_campaign/latest.md \
  --as-of-date 2026-06-16
```

El reporte consolida readiness, sesiones, ejecuciones, closeouts, alertas del
monitor, decisiones diarias si existen, performance paper si existe y ledger
JSONL. Debe mostrar sesiones completas, sesiones pendientes, blockers, ultimas
fechas, progreso contra 60 sesiones y `live_trading_authorized=false`. No
contacta broker, no lee credenciales, no envia Telegram, no descarga datos, no
recalcula senales y no autoriza live trading.

## 11. Cerrar el dia paper con decision auditable

Al final del dia, despues de readiness, corrida broker paper-confirmed, monitor
y campaign report, escriba el journal diario:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-day-close \
  --readiness reports/tmp/paper_daily_prepare/core_etfs/1d/2026-06-16/readiness.json \
  --broker-run reports/tmp/paper_daily_prepare/core_etfs/1d/2026-06-16/paper_daily/broker_confirmed/broker_run.json \
  --monitor reports/tmp/paper_monitor/latest.json \
  --campaign-report reports/tmp/paper_campaign/latest.json \
  --operator operador-paper \
  --reason "revision diaria paper" \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

El comando escribe `reports/tmp/paper_decisions/<as_of_date>/decision.json` y
`.md`, incluye rutas y hashes SHA-256 de los artefactos usados, y solo permite
`CONTINUE`, `REVIEW`, `STOP` o `ERROR`. `monitor=OK` permite `CONTINUE`,
`WARN` exige `REVIEW`, `CRITICAL` exige `STOP` y artefactos faltantes o JSON
invalido producen `ERROR` con salida `2`. JSON, Markdown y ledger se redactan
para no conservar tokens, API keys o secretos. No contacta broker ni autoriza
live trading.

## 12. Revisar performance y statement broker

Si exporto manualmente un statement paper del broker, validelo primero sin
contactar broker:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-statement-validate \
  --statement /ruta/local/alpaca-paper-statement.csv \
  --as-of-date 2026-06-16
```

El comando escribe
`reports/tmp/paper_statements/<as_of_date>/statement.normalized.json` y `.md`.
Acepta CSV/JSON local, exige `client_order_id`, `symbol`, `side`, `quantity`,
`filled_avg_price`, `filled_at` y `realized_pnl`, acepta aliases comunes de CSV
broker, rechaza duplicados de `client_order_id`, marca `WARN` si `filled_at`
no trae zona horaria o no cae en `--as-of-date`, preserva campos extra bajo
`raw` redacted, no lee credenciales y no conecta broker. Use el statement
normalizado para reconciliacion cuando exista.

Para revisar brecha paper-vs-backtest sin ampliar riesgo:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-performance-report \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --backtest-report reports/tmp/approved_eval/core_etfs/1d/2026-06-16/backtest.json
```

El reporte queda bajo `reports/tmp/paper_performance/`, marca PnL como `proxy`
si no viene de statement broker, advierte por precios faltantes y trata
`PENDING`/`UNMATCHED` como blockers de performance estable. No autoriza live.

Si exporto manualmente un statement paper del broker, use el input opcional:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-performance-report \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --backtest-report reports/tmp/approved_eval/core_etfs/1d/2026-06-16/backtest.json \
  --broker-statement reports/tmp/paper_statements/2026-06-16/statement.normalized.json
```

Un statement valido cambia `paper_metrics.pnl.source` a `broker_statement`.
Si falta el archivo, el reporte mantiene `proxy` y agrega warning. Si el
statement es invalido, escribe `status=ERROR` con salida `2`. Diferencias entre
closeouts locales y statement se reportan como missing fill, mismatch de
cantidad, precio, simbolo o fecha. El comando no conecta broker ni lee
credenciales.

## 13. Check operativo antes del siguiente submit

Antes de abrir el siguiente ciclo de submit paper, ejecute el verificador
read-only:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-ops-check \
  --as-of-date 2026-06-16 \
  --readiness-root reports/tmp/paper_daily_prepare \
  --sessions-root reports/tmp/paper_session \
  --monitor-root reports/tmp/paper_monitor \
  --campaign-root reports/tmp/paper_campaign \
  --decisions-root reports/tmp/paper_decisions \
  --performance-root reports/tmp/paper_performance \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl
```

El reporte queda en `reports/tmp/paper_ops_check/<as_of_date>/ops_check.json`
y `.md`. `OK` exige readiness `READY`, monitor sin criticos, campaign presente,
decision `CONTINUE`, performance presente y sin closeouts `PENDING` o
`UNMATCHED`. Falta de performance, decision `REVIEW`, warnings recurrentes o
statement ausente son `WARN`. Decision `STOP`, monitor/campaign criticos o
closeouts pendientes/unmatched son `CRITICAL`. JSON requerido invalido retorna
`ERROR` con salida `2`. No conecta broker, no lee credenciales y no cambia
modelos.

Para ensayar la rutina completa sin broker ni datos externos, use fixtures
deterministicos:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-ops-rehearsal \
  --as-of-date 2026-06-16 \
  --scenario complete
```

El rehearsal escribe bajo `reports/tmp/paper_rehearsal/<as_of_date>/`, valida
statement, genera performance, ops check, weekly summary y una decision humana
`DEFER` de challenger. Escenarios soportados: `complete`,
`missing-performance`, `stop` e `invalid-statement`. No lee credenciales, no
conecta broker y no envia ordenes.

Para ubicar la evidencia diaria antes del siguiente submit:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-evidence-index \
  --as-of-date 2026-06-16
```

El indice queda en
`reports/tmp/paper_evidence_index/<as_of_date>/evidence_index.json` y `.md`.
Resume readiness, monitor, campaign, decision, performance, ops check, weekly
summary, statement y decision challenger. Faltantes opcionales son `WARN`;
JSON requerido invalido es `ERROR` con salida `2`.

## 14. Resumen semanal paper

Al cierre de semana, consolide decisiones, campaign/performance y ledgers:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-weekly-summary \
  --decisions-root reports/tmp/paper_decisions \
  --performance-root reports/tmp/paper_performance \
  --campaign-root reports/tmp/paper_campaign \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --week 2026-W25 \
  --history-weeks 4
```

El resumen queda en `reports/tmp/paper_weekly_summary/<week>/`. Cinco dias
`CONTINUE` sin blockers producen `OK`; cualquier `STOP` produce `CRITICAL`;
`REVIEW` recurrente produce `WARN`; JSON invalido en decisiones produce
`ERROR` con salida `2`. `blocker_aging` cuenta blockers por semana, recurrencia,
dias consecutivos `REVIEW`, dias desde el ultimo `CONTINUE` y ultimos
`STOP`/`ERROR`. JSON historico invalido fuera de la semana actual se reporta
como warning; JSON invalido de la semana actual sigue siendo `ERROR`. JSON y
Markdown se redactan.

## 15. Gobierno champion/challenger

Para decidir si un challenger merece revision humana sin reemplazar el champion:

```bash
PYTHONPATH=src python3 -m trading_ai.cli model-challenger-report \
  --evaluation-dir reports/tmp/approved_eval/core_etfs/1d/2026-06-16 \
  --paper-performance reports/tmp/paper_performance/latest.json
```

El reporte queda bajo `reports/tmp/model_challenger/` y clasifica
`REVIEWABLE`, `REJECTED`, `BLOCKED` o `ERROR`. Requiere evidencia OOS,
costos, trades suficientes, sin leakage, drawdown tolerable y performance
paper compatible. Un `REVIEWABLE` solo habilita revision humana: ningun comando
muta `models/latest_model.json` ni reemplaza automaticamente el champion.

Registre la decision humana por separado:

```bash
PYTHONPATH=src python3 -m trading_ai.cli model-review-decision \
  --challenger-report reports/tmp/model_challenger/challenger_report.json \
  --decision DEFER \
  --reviewer operador-paper \
  --reason "esperar mas evidencia paper"
```

`APPROVE_FOR_NEXT_PAPER_CYCLE` solo se acepta si el challenger report esta
`REVIEWABLE`. `REJECT` y `DEFER` aceptan reportes `REVIEWABLE`, `REJECTED` o
`BLOCKED`. El comando escribe
`reports/tmp/model_challenger_decisions/<date>/decision.json` y `.md`, incluye
hashes de artefactos, redacta secretos y nunca muta `models/latest_model.json`.

Para cerrar el ciclo de revision sin promocion automatica:

```bash
PYTHONPATH=src python3 -m trading_ai.cli model-review-cycle-report \
  --challenger-report reports/tmp/model_challenger/challenger_report.json \
  --review-decision reports/tmp/model_challenger_decisions/2026-06-18/decision.json
```

El reporte queda en `reports/tmp/model_challenger_cycles/<date>/cycle_report.*`
y recomienda `READY_FOR_NEXT_PAPER_CYCLE`, `REJECTED_NO_PROMOTION` o
`DEFERRED`. Incluye hashes y mantiene `mutates_latest_model=false`.

Snapshot broker read-only opcional:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --as-of-date 2026-06-16 \
  --broker-read-only \
  --confirm-paper
```

`--broker-read-only` exige `--confirm-paper`. Sus defaults son
`--universe configs/universe.yml`, `--risk configs/risk.yml` y
`--order-status open`. El comando construye un cliente Alpaca paper con las
credenciales del entorno del proceso, consulta solo cuenta, posiciones
allowlisted y ordenes abiertas, y no envia, cierra ni cancela ordenes. Si faltan
credenciales, dependencia opcional o la API falla, escribe artefactos con
`broker_snapshot.status=ERROR`, razon redacted y retorna `2`.

Telegram es opt-in y esta apagado por defecto. Para verificar el mensaje sin
leer entorno ni usar red:

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --telegram-dry-run
```

Para enviar una notificacion real, exporte solo variables del proceso actual:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
PYTHONPATH=src python3 -m trading_ai.cli paper-monitor \
  --sessions-root reports/tmp/paper_session \
  --ledger-input reports/tmp/paper_observability/ledger.jsonl \
  --send-telegram
```

Por defecto se envian solo alertas criticas. Agregue
`--telegram-send-warnings` para incluir advertencias. El envio usa texto plano
por HTTPS contra Telegram Bot API `sendMessage` con `chat_id` y `text`; no usa
Markdown/HTML. El comando no lee `.env`, no guarda tokens, no guarda URLs
completas con token y no guarda respuestas completas de Telegram. Los artefactos
se escriben antes de intentar el envio; si Telegram falla con
`--send-telegram`, el comando retorna `2` y registra una razon redacted.
