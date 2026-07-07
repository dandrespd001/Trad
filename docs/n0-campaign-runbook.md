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
4. Identidad de reviewer para certificaciones: `dandrespd` (confirmada por
   el operador el 2026-07-06), usada en `autonomy-certify` y en resoluciones
   de incidentes.

## 2. Ciclo diario (INSTALADO 2026-07-06 como timer systemd de usuario)

Este sistema no tiene crontab; la programacion vive en systemd de usuario
(con linger habilitado, corre aunque no haya sesion abierta):

- Timer: `~/.config/systemd/user/trading-n0.timer` — dias habiles 16:45
  America/Bogota (`systemctl --user list-timers trading-n0.timer`).
- Servicio: `~/.config/systemd/user/trading-n0.service` → launcher
  `~/.config/trading-ai/run-n0-campaign.sh`.
- Log: `reports/tmp/cron_paper_auto.log`.

El launcher falla cerrado con mensaje claro (exit 3) si falta cualquiera de:

1. Credenciales en `~/.config/trading-ai/paper.env` (placeholders creados,
   chmod 600; verificado 2026-07-06 que NO estaban en el shell profile).
2. CSV de datos aprobados en `data/incoming/fresh_source.csv` (descarga
   manual del operador; el launcher exige que tenga < 5 dias).

Con los prerrequisitos presentes ejecuta el wrapper canonico:
`AUTONOMY_INCIDENT_SYNC=1 run-paper-auto-cycle.sh --source ... --dataset-id
core_etfs --frequency 1d --from <hoy-120d> --to <hoy> --as-of-date <hoy>
--license-note "manual download approved for paper use by dandrespd"
--confirm-paper-auto --require-clean-state`, y despues mide el progreso con
`paper-n0-certification` contra el ledger de sesiones
(`reports/tmp/paper_auto_cycle/session_ledger.jsonl`).

Notas:
- `--from` es el inicio de la ventana de DATOS (~120 dias moviles), no la
  fecha de operacion.
- `AUTONOMY_INCIDENT_SYNC=1` corre `autonomy-incident-sync` tras el ciclo
  (fuente kill switch); el wrapper sale con el peor exit code de ambos pasos
  (verificado en smoke test 2026-07-06).
- Prueba end-to-end 2026-07-06: `systemctl --user start trading-n0.service`
  ejecuto el launcher, dejo el BLOCKED esperado en el log y el fallo quedo
  visible en systemd — fail-closed operativo confirmado.

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
