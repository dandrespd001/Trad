# Runbook de promoción a dinero real — portafolio risk-parity ETF+cripto

Escrito 2026-07-09 (D2). Este documento registraba el procedimiento previsto
para avanzar hacia dinero real cuando Gate 1 cerrara. Desde 2026-07-14 no se
ejecuta: la promoción está bloqueada por P0-03. Ningún resultado pasado
garantiza rentabilidad futura (§0).

> **INVALIDATED_FOR_PROMOTION — 2026-07-14.** Las cifras de edge, DSR, PF,
> costos y envelope calculadas con el engine v1 son evidencia histórica y no
> sirven para aprobar promoción. P0-03 exige revalidarlas con ejecución
> next-open, costo all-in aplicado una sola vez, cash fijo entre sleeves y trial
> ledger. Hasta completar esa revalidación, cualquier gate de promoción queda
> **BLOQUEADO — REVALIDACIÓN REQUERIDA**. Los hechos operativos de órdenes y
> fills conservan su valor como evidencia operativa, no como validación de edge.

## 0. Qué está operando hoy (Gate 1, desde 2026-07-09)

- Timer `trading-crypto-sleeve.timer` (diario 19:05 local) → launcher
  `~/.config/trading-ai/run-crypto-sleeve.sh`:
  fetch gobernado cripto+ETF → `sleeve-allocate` ($20,000 — 20% de la cuenta, orden operador 2026-07-09, cap 1.0) →
  `sleeve-rebalance` cripto (mw=120) y ETF (mw=20) en cuenta PAPER →
  scorecard `sleeve-gate1-report` (read-only) → Telegram (autorizado).
- La estrategia figuraba como validada por la batería §4 (6/6) del engine v1;
  ese pase y los umbrales PF asociados son históricos y están invalidados para
  promoción hasta cerrar P0-03. Evidencia histórica: §28/§30/§31/§32 del doc
  maestro.
- Artefactos diarios: `reports/tmp/sleeve_rebalance/{allocation,cycle_crypto,
  cycle_etf,telegram_daily,telegram_send}_<fecha>.json`, `gate1_report.{json,md}`,
  log `reports/tmp/cron_crypto_sleeve.log`.

## 1. Criterio operativo histórico de Gate 1 (no habilita promoción)

> **Contrato vigente desde 2026-07-14:** el scorecard histórico sin política
> prerregistrada no es evidencia gobernada. El Gate nuevo exige política `2.0`
> y ledger append-only; los mínimos de fills y límites mediana/P90 se fijan por
> sleeve antes de la ventana. Incluso un `OK` operativo paper declara
> `economic_reconciliation_complete=false` y `promotion_eligible=false`. Véase
> [P0-04](remediation/p0-04-gate1-signed-costs-evidence-ledger.md).

Al cumplirse la ventana que el operador decida (los días no son comprimibles),
revisar `gate1_report.md` y exigir TODO lo siguiente:

1. **Cero incidentes críticos**: sin ciclos BLOCKED inexplicados, sin
   `order_lookup_failed`, sin errores distintos de la idempotencia esperada.
2. **Fills coherentes**: cada orden enviada terminó FILLED o expirada-DAY con
   causa clara; cero órdenes duplicadas o huérfanas (reconciliar contra
   posiciones).
3. **Coste efectivo histórico:** el límite global de 35 bps era el envelope
   del engine v1 y queda invalidado. El Gate vigente no lo usa: exige límites
   prerregistrados independientes por sleeve y conserva shortfall favorable
   con signo negativo. P0-03/P0-04 deben reconstruir y validar cualquier
   envelope antes de otra decisión.
4. **Comportamiento de estrategia correcto**: flat cuando momentum negativo
   (verificado día 1), compras solo con momentum positivo, pesos ≤ 0.10,
   `pending_buy_notional` evitando re-compras.
5. **PnL del período reportado tal cual** (positivo o no): con pocos días el
   PnL es ruido — este control es solo OPERATIVO; el edge de §28 ya no se
   considera validado para promoción y debe revalidarse bajo P0-03.

## 2. Promoción BLOQUEADA — revalidación requerida

No completar ni usar este checklist para autorizar capital real hasta cerrar
P0-03 y emitir evidencia nueva. Se conserva únicamente como referencia
histórica del control humano previsto.

- [ ] Leí `gate1_report.md` completo y los incidentes son cero o explicados.
- [ ] Existe política Gate 1 v2 prerregistrada, ledger íntegro y cobertura
      completa por sleeve; ningún rango histórico se usa como sustituto.
- [ ] Entiendo que la expectativa es Sharpe ~1.0-1.4 con maxDD ~4-7% (MC p95
      6.6%) y AÑOS NEGATIVOS posibles (2018, 2022 lo fueron), y que nada
      garantiza rentabilidad (§0).
- [ ] Defino el capital inicial real (recomendado: empezar canario, p.ej.
      1-2% del capital destinado, y escalar solo tras semanas limpias).
- [ ] Acepto los kill-switches vigentes (pérdida diaria 2%, drawdown 10%,
      posición máx 2% — `configs/risk.yml`) o los ajusto ANTES de promover.

## 3. Procedimiento técnico histórico (NO EJECUTAR hasta revalidar)

El repo fuerza `live_trading_allowed: false` con scanners. Históricamente, la
promoción exigía los cambios explícitos siguientes. P0-03 mantiene bloqueado
este procedimiento aunque el Gate 1 operativo cierre:

1. Crear cuenta/credenciales LIVE de Alpaca y ponerlas en un env NUEVO
   (`~/.config/trading-ai/live.env`) — NUNCA en el repo ni en logs.
2. Revisar `configs/risk.yml`: cambiar la bandera `live_trading_allowed` de
   `false` a verdadero SOLO en la config de despliegue aprobada (fuera del
   árbol del repo), con reviewer humano (dandrespd) — el scanner
   `verify-safety-patterns.py --mode live` caza ese patrón literal en el
   repo: si falla, es la confirmación de que alguien lo commiteó; nunca debe
   estar en el árbol.
3. El path histórico de ejecución live queda **inhabilitado por código** tras
   la reauditoría P0 del 2026-07-14. `live_canary` y `AlpacaLiveBroker` no
   ejecutan el cliente aunque se pasen flags de submit. La confirmación textual,
   `live_stage_policy`, breaker y reconciliación existentes no constituyen una
   autorización segura. Hace falta un ejecutor separado con intent firmado,
   journal, riesgo broker-first, rollback y reconciliación post-fill; véase el
   [NO-GO de frontera live](remediation/p0-live-boundary-no-go-2026-07-14.md).
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

- No promover: P0-03 bloquea la promoción incluso si se cumple el criterio
  operativo §1, hasta que exista revalidación explícita.
- No subir el leverage cap (>1.0 duplica el riesgo de cola — §27: maxDD 62.9%).
- No re-tunear parámetros con datos de Gate 1 (overfitting; §31 demostró que
  la selección adaptativa empeora).
- No operar clases fuera del universo validado (forex sin edge §24; futuros
  solapan §29).
