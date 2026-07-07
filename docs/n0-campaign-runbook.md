# Runbook: Campania de Evidencia N0 (equities)

**Fecha:** 2026-07-06
**Autor:** Fable 5 (Arquitecto/Lider Tecnico)
**Objetivo:** acumular >= 20 dias habiles paper limpios con PnL neto positivo
y drawdown < 10% para certificar `equities` de N0 a N1 (ver
`docs/autonomy-ladder.md`). Este runbook deja la operacion diaria lista; lo
unico que falta del operador son credenciales, el cron y su identidad de
reviewer.

## 1. Prerrequisitos (una sola vez)

1. Entorno local verde: `./scripts/verify-paper-environment.sh` y
   `./scripts/verify-release-minimal.sh`.
2. Credenciales paper de Alpaca en el entorno del proceso (nunca en archivos
   del repo):
   - `ALPACA_PAPER_API_KEY`
   - `ALPACA_PAPER_SECRET_KEY`
3. Bot de Telegram:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID` (el chat del operador; tambien es la allowlist de
     `telegram-control-*` junto con el user id del operador)
4. Identidad de reviewer para certificaciones (nombre estable, p. ej.
   `dandrespd`), usada en `autonomy-certify` y en resoluciones de incidentes.

## 2. Ciclo diario (cronable)

Una linea de cron (dias habiles, tras el cierre del mercado US, hora local
Bogota = UTC-5; 16:30 ET ~ 15:30 Bogota):

```
45 16 * * 1-5 cd /home/adquiod/Documentos/Algoritmic-IA && \
  AUTONOMY_INCIDENT_SYNC=1 PAPER_AUTO_REQUIRE_OPERATIONAL_EVIDENCE=1 \
  ./scripts/run-paper-auto-cycle.sh --as-of-date "$(date -u +\%F)" \
  --confirm-paper-auto --require-clean-state >> reports/tmp/cron_paper_auto.log 2>&1
```

Notas:
- `AUTONOMY_INCIDENT_SYNC=1` corre `autonomy-incident-sync` tras el ciclo
  (fuente kill switch); una degradacion nunca queda enmascarada.
- El wrapper ya exige entorno verde y estado limpio; si algo falla, el exit
  code lo refleja y el log lo documenta.

## 3. Status y control por Telegram

Status diario con la escalera visible:

```
PYTHONPATH=src python3 -m trading_ai.cli paper-telegram-status \
  --as-of-date <fecha> --autonomy-state-dir reports/tmp/autonomy \
  --n0-certification reports/tmp/n0_certification/equities/<fecha>/certification.json ...
```

y envio con `paper-telegram-send` (dry-run por defecto; `--send-telegram`
para envio real). Comandos entrantes (`/status`, `/pause`, `/flatten`,
`/approve`, `/veto`, kill switch) via `telegram-control-*` con allowlist de
`TELEGRAM_CHAT_ID` + user id.

## 4. Medicion semanal de la campania

Cada viernes (o al cierre de la semana):

```
PYTHONPATH=src python3 -m trading_ai.cli paper-n0-certification \
  --as-of-date <fecha> --market equities \
  --session-ledger <ledger(s) de paper-auto-cycle> \
  --performance-report reports/tmp/paper_performance/latest.json
```

- `ACCUMULATING`: seguir; el artefacto reporta `remaining_clean_days`.
- `BLOCKED`: aplicar la regla del goal — detener features, aislar la causa,
  test de regresion, y solo despues continuar la campania.
- `CERTIFIED_READY`: el artefacto imprime el comando exacto de
  `autonomy-certify` con hash y dias; la ejecucion de ese comando es una
  DECISION HUMANA documentada (reviewer + reason).

## 5. Transicion a N1 (cuando CERTIFIED_READY)

1. Operador ejecuta el `suggested_certify_command` del certificado con su
   reviewer/reason.
2. El canary real USD 1 sigue exigiendo TODO el camino S12 existente (doble
   confirmacion exacta, risk runtime, precio de referencia, breaker limpio,
   rollback prevalidado) MAS los gates nuevos: nivel N1 certificado sin
   incidente abierto y plan de senal aprobado via `/approve` sin veto.
3. >= 10 dias reales limpios en N1 alimentan la certificacion a N2 (ventana
   de veto), con `evidence_kind` `real_canary_certification`.

## 6. Reglas permanentes

- Un incidente grave degrada un nivel automaticamente; se resuelve solo con
  `autonomy-resolve-incident` (reviewer + reason) y recertificacion.
- Ningun artefacto de esta campania autoriza trading live por si mismo; los
  scanners de seguridad deben salir limpios en cada cambio de codigo.
- Divergencia sostenida entre lo esperado y lo observado degrada nivel
  (revision semanal del Arquitecto en N1+).
