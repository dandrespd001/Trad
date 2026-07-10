# Runbook de promoción a dinero real — portafolio risk-parity ETF+cripto

Escrito 2026-07-09 (D2). Este documento convierte el objetivo final del goal
("que el sistema funcione correctamente con dinero real") en el procedimiento
operativo concreto que ejecutará el OPERADOR cuando Gate 1 cierre. La promoción
es una decisión humana por diseño: ningún paso de este runbook la automatiza, y
ningún resultado pasado garantiza rentabilidad futura (§0).

## 0. Qué está operando hoy (Gate 1, desde 2026-07-09)

- Timer `trading-crypto-sleeve.timer` (diario 19:05 local) → launcher
  `~/.config/trading-ai/run-crypto-sleeve.sh`:
  fetch gobernado cripto+ETF → `sleeve-allocate` ($1000, cap 1.0) →
  `sleeve-rebalance` cripto (mw=120) y ETF (mw=20) en cuenta PAPER →
  scorecard `sleeve-gate1-report` (read-only) → Telegram (autorizado).
- Estrategia validada: batería §4 completa (6/6) con umbrales confirmados por
  el operador (cripto aceptada; PF ~1.2 aceptado, gate operativo PF ≥ 1.15).
  Evidencia: §28/§30/§31/§32 del doc maestro.
- Artefactos diarios: `reports/tmp/sleeve_rebalance/{allocation,cycle_crypto,
  cycle_etf,telegram_daily,telegram_send}_<fecha>.json`, `gate1_report.{json,md}`,
  log `reports/tmp/cron_crypto_sleeve.log`.

## 1. Criterio de cierre de Gate 1 (duración autorizada: <16 días)

Al cumplirse la ventana que el operador decida (los días no son comprimibles),
revisar `gate1_report.md` y exigir TODO lo siguiente:

1. **Cero incidentes críticos**: sin ciclos BLOCKED inexplicados, sin
   `order_lookup_failed`, sin errores distintos de la idempotencia esperada.
2. **Fills coherentes**: cada orden enviada terminó FILLED o expirada-DAY con
   causa clara; cero órdenes duplicadas o huérfanas (reconciliar contra
   posiciones).
3. **Costo efectivo medido ≤ 35 bps** en cripto (mediana de
   `effective_cost_bps`): es el escenario donde el edge aguanta (§30, Sharpe
   1.017/1.282). Si la mediana supera ~45 bps, el edge full-sample cae
   sub-gate (0.947) → NO promover; investigar ejecución (horario, tamaño,
   límites) antes.
4. **Comportamiento de estrategia correcto**: flat cuando momentum negativo
   (verificado día 1), compras solo con momentum positivo, pesos ≤ 0.10,
   `pending_buy_notional` evitando re-compras.
5. **PnL del período reportado tal cual** (positivo o no): con pocos días el
   PnL es ruido — el gate es OPERATIVO (el edge ya está validado en §28); no
   exigir rentabilidad en días, no cancelar por ruido de días.

## 2. Decisión humana de promoción (checklist del operador)

- [ ] Leí `gate1_report.md` completo y los incidentes son cero o explicados.
- [ ] El costo efectivo cripto medido está en el rango donde el edge aguanta.
- [ ] Entiendo que la expectativa es Sharpe ~1.0-1.4 con maxDD ~4-7% (MC p95
      6.6%) y AÑOS NEGATIVOS posibles (2018, 2022 lo fueron), y que nada
      garantiza rentabilidad (§0).
- [ ] Defino el capital inicial real (recomendado: empezar canario, p.ej.
      1-2% del capital destinado, y escalar solo tras semanas limpias).
- [ ] Acepto los kill-switches vigentes (pérdida diaria 2%, drawdown 10%,
      posición máx 2% — `configs/risk.yml`) o los ajusto ANTES de promover.

## 3. Procedimiento técnico de promoción (cuando el operador decida)

El repo fuerza `live_trading_allowed: false` con scanners; la promoción exige
cambios explícitos que dejan rastro:

1. Crear cuenta/credenciales LIVE de Alpaca y ponerlas en un env NUEVO
   (`~/.config/trading-ai/live.env`) — NUNCA en el repo ni en logs.
2. Revisar `configs/risk.yml`: fijar `live_trading_allowed: true` SOLO en la
   rama/config de despliegue aprobada, con reviewer humano (dandrespd) — el
   scanner `verify-safety-patterns.py --mode live` está diseñado para
   detectar esto: su fallo es la CONFIRMACIÓN de que la decisión fue
   explícita, no un olvido.
3. El path de ejecución live existente (`live_canary`, `live_stage_policy`,
   `live_circuit_breaker`, `live_reconciliation`) exige etapa CANARY con
   confirmación humana; el sleeve-rebalance actual es paper-only
   (`AlpacaPaperBroker`) — la extensión a broker live es un sprint propio
   con su spec, revisión y gates (NO improvisar cableado).
4. Empezar con el MISMO portafolio y presupuesto proporcional pequeño;
   mantener el ciclo paper en paralelo (shadow) al menos 2 semanas para
   comparar fills paper vs live (slippage real).
5. Vigilancia diaria por Telegram + `gate1_report` (funciona igual contra la
   cuenta live cambiando credenciales del env; sigue siendo read-only).

## 4. Reversión (siempre disponible)

- Parar el timer: `systemctl --user disable --now trading-crypto-sleeve.timer`.
- Aplanar posiciones: `close_all_positions` vía procedimiento del runbook de
  recovery (`docs/runbook-recovery-rollback.md`).
- Volver a paper: restaurar env/paper y `live_trading_allowed: false`.

## 5. Qué NO hacer

- No promover por impaciencia antes del criterio §1.
- No subir el leverage cap (>1.0 duplica el riesgo de cola — §27: maxDD 62.9%).
- No re-tunear parámetros con datos de Gate 1 (overfitting; §31 demostró que
  la selección adaptativa empeora).
- No operar clases fuera del universo validado (forex sin edge §24; futuros
  solapan §29).
