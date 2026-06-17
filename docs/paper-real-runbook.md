# Runbook: Alpaca Paper Real Controlado

Este runbook describe el unico flujo permitido para enviar una orden real contra
Alpaca paper desde un paquete `paper-session` aprobado. No habilita live trading,
no descarga datos, no recalcula senales y no modifica el paquete offline
aprobado.

## Flujo diario resumido

Para una corrida manual diaria o un cron paper-only, mantenga esta secuencia:

1. Genere o actualice `paper-session` con datos aprobados y notional fijo USD
   `1.0`.
2. Si aplica y despues de revision manual, ejecute `paper-execute-session`.
3. Si existe ejecucion enviada, cierre evidencia con `paper-close-session`
   hasta obtener `CLOSED` o resolver `PENDING`/`UNMATCHED`.
4. Consolide evidencia con `paper-observability`.
5. Ejecute `paper-monitor` y detenga acciones paper si retorna `CRITICAL`.

Ningun paso de este flujo habilita live trading. El monitor tampoco contacta
Alpaca, no lee credenciales broker, no envia ordenes, no cancela ordenes, no
descarga datos, no recalcula senales y no cambia modelos.

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
  --output reports/alpaca_signal_order_reconciliation.json \
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

```bash
PYTHONPATH=src python3 -m trading_ai.cli paper \
  --broker alpaca \
  --real-paper \
  --confirm-paper \
  --get-order \
  --client-order-id signal-spy-20260616 \
  --output reports/alpaca_order_signal_spy.json \
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
  --output reports/alpaca_cancel_signal_spy.json \
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
  --ledger-output reports/tmp/paper_observability/ledger.jsonl
```

Tambien puede incluir sesiones concretas con `--session-dir <dir>` repetible.
El monitor reusa la observabilidad offline y escribe:

- `reports/tmp/paper_monitor/latest.json`
- `reports/tmp/paper_monitor/latest.md`

La salida esperada es:

- `0` con `status=OK`: no hay alertas.
- `0` con `status=WARN`: hay advertencias, por ejemplo ledger solicitado
  faltante, ausencia de sesiones recientes o evidencia incompleta no critica.
- `1` con `status=CRITICAL`: detenga acciones paper hasta resolver los
  bloqueos.
- `2`: error operativo, por ejemplo fallo al escribir artefactos o fallo de
  Telegram cuando `--send-telegram` esta activo.

Alertas criticas:

- diagnosticos de observabilidad por artefactos faltantes o JSON invalido;
- sesiones bloqueadas;
- ejecuciones bloqueadas;
- closeout `PENDING` o `UNMATCHED`;
- ejecucion `SUBMITTED` sin closeout;
- blockers existentes en observabilidad.

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
