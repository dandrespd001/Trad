# Handover post-2026-07-12 (operador sin Fable)

El sistema corre solo. Esto es lo mínimo que necesitas saber para operarlo,
vigilarlo y avanzar hacia dinero real sin el Arquitecto.

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

## Cuando Gate 1 cierre (tú decides la fecha, <16 días desde 2026-07-09)
1. Lee `docs/live-promotion-runbook-sleeves.md` (criterio §1, checklist §2).
2. Si decides promover: despacha el sprint live YA ESPECIFICADO:
   `bash ~/.config/trading-ai/minimax-exec.sh reports/tmp/specs/spec-m6-live-broker-extension.md`
   (el spec tiene gate humano interno: confirma en el prompt que Gate 1 cerró
   y que decidiste promover). Revisa el diff con el checklist del spec y corre:
   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -q`
   `bash scripts/verify-release-minimal.sh`
   Ambos deben quedar verdes antes de commitear.
3. Sigue el runbook §3 (env live separado, canario pequeño, shadow paper
   2 semanas). §4 tiene la reversión completa.

## Parar todo
`systemctl --user disable --now trading-crypto-sleeve.timer` y, si hay
posiciones, el flatten del runbook de recovery
(`docs/runbook-recovery-rollback.md`).

## Estado al 2026-07-09
Suite 1312 tests verde. Edge validado batería completa con tus umbrales
(cripto sí, PF ~1.2). Gate 1 día 1-2 sin incidentes (2 bugs cazados y
corregidos: exposición pendiente y fecha UTC/local). Nada garantiza
rentabilidad; la evidencia está en
`docs/evidence-2026-07-08-extended-champion-decision.md` §§28-32.
