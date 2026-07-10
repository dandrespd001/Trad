# Runbook — Parada, recuperación tras fallo y rollback

Entregable DoD §7.5 (complemento de `docs/paper-real-runbook.md` y
`docs/n0-campaign-runbook.md`, que cubren arranque y operación diaria). Todos
los comandos referenciados fueron verificados en el repo (2026-07-08). Regla
permanente: **ninguna orden real ni cambio paper→live sin confirmación humana
explícita**; live está `false` por defecto y el scanner lo fuerza.

## 1. Parada de emergencia (kill / flatten)

- **Aplanar todo en paper y detener** (fail-closed):
  `trading-ai paper-safe-flatten` — cierra posiciones abiertas de forma
  conservadora. Añadir `--reset-kill-switch-after` solo para rearmar el sistema
  tras resolver la causa (decisión humana).
- **Equivalente live** (solo si alguna vez se habilita, con confirmación humana):
  `trading-ai live-safe-flatten`.
- **Vía Telegram:** `/kill` y `/close_all` requieren **doble confirmación**
  (`requires_confirmation` + confirm explícito) y están restringidos por la
  whitelist de `chat_id` (`execution/telegram_control.py`).

## 2. Recuperación tras reinicio del proceso

El estado crítico se persiste con checksum de integridad
(`integrity_sha256`, `execution/paper_risk_state.py`) y lectura **fail-closed**:
si el checksum no cuadra, el sistema NO opera. Pasos:

1. Reconciliar contra el broker antes de cualquier acción: la reconciliación
   está integrada en `trading-ai paper-execute-session` (módulo
   `execution/live_reconciliation.py`), que compara posiciones y órdenes reales
   vs. el estado persistido y reporta divergencias (`fill_timeout`,
   `partial_fill`, etc.). No es un subcomando standalone.
2. Consolidar observabilidad: `trading-ai paper-observability`.
3. Si hubo un incidente de autonomía, resolverlo de forma auditada:
   `trading-ai autonomy-resolve-incident` (deja ledger append-only).
4. Solo tras divergencias en cero y checksum válido, reanudar el ciclo
   (`paper-daily` / timer `trading-n0.timer`).

Regla dura: si la reconciliación reporta divergencias no explicadas, el ciclo
diario ya falla cerrado (`monitor_before_submit_critical`) — no forzar submisión.

## 3. Rollback

- **Código:** cada sprint va en la rama de feature con gate verde
  (`verify-release-minimal.sh`). Rollback = `git revert <commit>` del sprint
  ofensor y re-correr el gate; `models/latest_model.json` es inmutable-gateado,
  así que un rollback de código nunca cambia el campeón por accidente.
- **Estado de riesgo/posición:** los artefactos de estado son append-only o
  versionados con checksum; un rollback de estado se hace restaurando el último
  snapshot con `integrity_sha256` válido y re-reconciliando (paso §2.1) — nunca
  editando el JSON a mano (rompería el checksum y el sistema fallaría cerrado,
  que es lo correcto).
- **Configuración de riesgo:** `configs/risk.yml` está versionado; revertir un
  cambio de límites es un commit normal + gate. `live_trading_allowed` debe
  permanecer `false`.

## 4. Operación vía Telegram (resumen de seguridad)

Comandos de control (`/status /pause /resume /close /close_all /risk /mode /kill
/report`) pasan por el inbox auditado (`telegram_control.py`): whitelist de
`chat_id`, doble confirmación en acciones destructivas, y ledger de auditoría.
El token vive solo en `~/.config/trading-ai/paper.env` (nunca en repo ni logs;
redacción recursiva E1–E2). Estado y PnL: `paper-telegram-status`,
`paper-telegram-history`, `paper-telegram-send`.

## 5. Checklist de recuperación (orden estricto)

1. `paper-safe-flatten` si hay exposición dudosa.
2. Reconciliar (paso §2.1) → divergencias deben ser 0.
3. Verificar checksum de estado válido; si no, restaurar snapshot y re-reconciliar.
4. `autonomy-resolve-incident` para cualquier incidente abierto.
5. `verify-release-minimal.sh` verde antes de reanudar.
6. Reanudar ciclo solo con TODO lo anterior en verde. Ante duda: parar y escalar
   al operador (nunca forzar).

## Recuperación del ciclo sleeve (portafolio §28, verificado 2026-07-10)

Propiedades de recuperación verificadas en vivo:
1. **Re-ejecución segura el mismo día** (crash a mitad de ciclo → re-lanzar):
   los `client_order_id` son deterministas por (fecha, par, lado); el broker
   rechaza duplicados (40010001) y el ciclo los reporta como WARN limpio sin
   duplicar exposición. Verificado 2026-07-09 y 07-10 con órdenes reales.
2. **Órdenes en vuelo entre ciclos**: `pending_buy_notional` cuenta las
   compras abiertas como exposición — un ciclo tras un crash no re-compra.
3. **Runs perdidos** (máquina apagada a las 19:05): ambos timers tienen
   `Persistent=true` + linger activo (verificado `Linger=yes`, ambos
   `enabled`) → systemd ejecuta el run perdido al encender.
4. **Estado corrupto**: high-water ilegible → se reconstruye desde el equity
   actual (drawdown 0 ese día — conservador hacia no-bloquear);
   `breaker_state.json` ilegible → stage none (documentado en el módulo: el
   trade-off evita quedar atrapado en pausa por un glitch de I/O; la pausa
   REAL la restituye el siguiente chequeo del breaker si el límite sigue roto).
5. **Despause del breaker**: SOLO humano — borrar
   `reports/tmp/sleeve_rebalance/breaker_state.json` tras revisar causa.
Comandos de parada/arranque: `systemctl --user {stop|start|disable|enable}
trading-crypto-sleeve.timer trading-position-watch.timer`.
