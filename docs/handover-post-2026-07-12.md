# Handover post-2026-07-12 (operador sin Fable)

El sistema corre solo. Esto es lo mínimo que necesitas saber para operarlo,
vigilarlo y avanzar hacia dinero real sin el Arquitecto.

> **INVALIDATED_FOR_PROMOTION — 2026-07-14.** Las cifras de edge, DSR, PF,
> costos y envelope calculadas con el engine v1 son evidencia histórica y no
> sirven para aprobar promoción. P0-03 exige revalidarlas con ejecución
> next-open, costo all-in aplicado una sola vez, cash fijo entre sleeves y trial
> ledger. Hasta completar esa revalidación, cualquier gate de promoción queda
> **BLOQUEADO — REVALIDACIÓN REQUERIDA**. Los hechos operativos de órdenes y
> fills conservan su valor como evidencia operativa, no como validación de edge.

## Qué corre automáticamente
- **Diario 19:05** (`trading-crypto-sleeve.timer`): fetch de datos gobernado →
  asignación risk-parity → rebalanceo cripto y ETF en cuenta PAPER →
  scorecard → mensaje a tu Telegram.
- Si un día no llega el Telegram: mira
  `reports/tmp/cron_crypto_sleeve.log` y `systemctl --user status
  trading-crypto-sleeve.service`.

## Qué mirar cada pocos días
- `reports/tmp/sleeve_rebalance/gate1_report.md` — días acumulados, fills,
  costo efectivo (bps), incidentes, posiciones, equity.
- Regla §0: el PnL de pocos días es ruido; lo que importa en Gate 1 es CERO
  incidentes críticos y costo cripto medido ≤ ~35 bps.

## Cuando cierre la observación operativa de Gate 1
1. Lee `docs/live-promotion-runbook-sleeves.md` (criterio §1, checklist §2).
2. **No despaches el sprint live ni cambies configuración de promoción.** El
   cierre operativo no supera el bloqueo P0-03; primero deben existir resultados
   next-open, costos all-in una vez, cash fijo y trial ledger revalidados.
3. El runbook §3 queda como referencia histórica y **NO SE EJECUTA** hasta que
   una nueva evidencia levante explícitamente el bloqueo.

## Parar todo
`systemctl --user disable --now trading-crypto-sleeve.timer` y, si hay
posiciones, el flatten del runbook de recovery
(`docs/runbook-recovery-rollback.md`).

## Estado al 2026-07-09
Suite 1312 tests verde. El edge figuraba como validado con el engine v1 y PF
~1.2, pero esa evidencia está invalidada para promoción por P0-03. Gate 1 día
1-2 sin incidentes (2 bugs cazados y
corregidos: exposición pendiente y fecha UTC/local). Nada garantiza
rentabilidad; la evidencia está en
`docs/evidence-2026-07-08-extended-champion-decision.md` §§28-32.
