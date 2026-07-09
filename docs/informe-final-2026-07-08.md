# Informe honesto de cierre — D1-D2 (2026-07-08/09)

Entregable DoD §7.7. Este informe reporta **solo** métricas de código ejecutado
en la sesión, con rutas a artefactos. Sin lenguaje de "rentabilidad garantizada"
(prohibido por el goal). Lo que no se pudo demostrar se marca como tal.

## 1. Resumen ejecutivo (estado decisivo al cierre de D2)

La **infraestructura** (gestión dinámica, riesgo, kill-switch, Telegram con
seguridad, validación estadística, ejecución paper con fills reales) está
mayormente implementada y testeada (~1246 tests). Sobre el **edge**, tras una
investigación multi-clase exhaustiva y honesta (ETF, forex, cripto, futuros;
2005-2026; walk-forward + DSR + Monte Carlo + costos realistas):

- **Único edge que pasa el gate de Sharpe:** portafolio **risk-parity ETF+cripto**
  con vol-targeting (§28, código L1/L2): Sharpe **1.088 full / 1.380 OOS**, MaxDD
  3.8%, MC p95 6.6%, DSR 0.995, 7/9 años positivos, walk-forward todos positivos.
  Pasa **5 de 6 gates §4**; **falla solo Profit Factor** (1.205, techo real ~1.2 <1.3).
- **Depende de cripto**, que NO está en el scope explícito del goal (ETF/Forex/
  Futuros). Dentro del scope explícito NO hay edge gate-passing: ETF solo Sharpe
  0.79; ETF+futuros 0.75 (los futuros solapan con ETF, corr 0.48; §29); forex sin
  edge. La unicidad de cripto (corr 0.11) es lo que activa el edge.
- El filtro de régimen (que parecía prometedor en 5 años) fue **REFUTADO sobre 21
  años** (§23) — era artefacto de ventana corta. El ML direccional no tiene edge.

**Nada es promocionable a live** todavía, y esa decisión es humana. El camino
depende de TRES decisiones del operador (§7c).

## 1a. Tres decisiones del operador que determinan el cierre

1. **¿Aceptar cripto?** → **DECIDIDO SÍ (2026-07-09, goal nuevo "cripto con
   Alpaca")**. Integración completa M1-M3: ingesta gobernada (provider
   `alpaca_crypto_data`, universo 6 pares), broker cripto-aware (24/7, GTC,
   mínimo $10, price-sanity cripto), y ciclo `sleeve-rebalance` que ejecuta la
   estrategia validada contra la cuenta paper. Verificado con fills reales
   ($10 BTC/USD FILLED→flat). Sensibilidad §30: a costo Alpaca realista (35bps)
   el edge aguanta (Sharpe 1.017/1.282), margen fino.
2. **¿PF de 1.2 es aceptable** vs la guía "PF ≥ ~1.3" (goal escribe "~" y
   "confirmar")? Con Sharpe 1.09 + DSR 0.995 + DD 3.8% es una estrategia sólida.
   PENDIENTE.
3. **¿Arrancar Gate 1** en paper? → Ciclo diario listo (launcher
   `run-crypto-sleeve.sh`, $500); falta aprobar el timer systemd y correr los
   días reales (<16). El primer ciclo real (2026-07-09) decidió FLAT
   honestamente (momentum cripto 120d negativo en los 6 pares).

## 1b. Corrección de honestidad crítica (evidencia §18-23)

La sesión pasó por dos correcciones honestas encadenadas: (1) el scorecard
completo §20 mostró que el régimen falla PF y Sharpe-full aun en 5 años; (2) la
validación sobre 21 años (§23, habilitada por el desbloqueo de datos del operador)
**refuta** el edge de régimen — el Sharpe 1.07 OOS de §18 era un artefacto de la
ventana corta 2021-2026. Lección central: **5 años no bastan** para validar un
overlay que actúa sobre eventos de cola raros; se necesitan múltiples ciclos de
crash. El goal §0 exige exactamente esto: la validación más honesta (historia
larga) destruyó el número bonito, y se reporta así. No hay edge promocionable.

## 2. Qué se hizo en D1 (11 sprints/evidencias, todo con gate verde)

| Entrega | Commit | Qué |
| --- | --- | --- |
| G3 | 3e1fa0a | `train --feature-names` opt-in |
| Decisión promoción | b633a4b | RECHAZADA (proxy activación no da edge) |
| H1 | afb3cff | estandarización train-only (log_loss 15→1.7) |
| H2 | 875bda3 | triple-barrier + embargo anti-leakage |
| I1 | fcc47c4 | LightGBM no-lineal (confirma no-edge) |
| I2 | 8eb159f | features cross-sectional |
| J1 | db5ecc0 | PSR + DSR + Monte Carlo |
| Backtest reglas | f04676a | Sharpe 0.46 con costos |
| Validación §4 | b96d4aa | PSR 0.83, colas gordas |
| Sensibilidad ±20% | f22b419 | maxDD ~1% robusto |
| Intradía (opción 3) | 07ebab3 | horario real IEX, tampoco edge |
| Checklist gates | 65056ba | §7.6 |

Roles: MiniMax implementó G3/H1/H2/I2 (revisados y aprobados por el Arquitecto);
el Arquitecto implementó I1 (sandbox de MiniMax sin deps ML) y J1 (MiniMax cayó
por cuota 429 — plan B del goal).

## 3. Hallazgo central (evidencia reproducible)

Matriz completa probada sobre 5 años reales:
**{diario, horario} × {time-series base/extended, cross-sectional} ×
{logístico ±estandarización, LightGBM} × {direction, triple-barrier}**.
**Ninguna combinación supera al naive always-long OOS.** LightGBM memoriza el
train (acc 0.78-0.83 diario) pero generaliza a ~0.50 — firma de ausencia de
señal aprendible. El techo es el contenido de información de las features/target,
no la clase de modelo. Detalle en `docs/evidence-2026-07-08-extended-champion-decision.md` §§1-15.

## 4. Lo único con expectativa positiva

Estrategia de reglas `momentum-vol-target` (5 años, con costos 1bp+1bp):

- Sharpe 0.46, Sortino 0.44, **MaxDD 1.1%** (control de riesgo excelente).
- PSR(SR>0) 0.826; Monte Carlo maxDD p95 2.96% / peor 6.1% (≤15% ✓).
- Sobrevive ±20% de perturbación (Sharpe 0.43-0.73, maxDD ~1%).
- ⚠️ Distribución severamente no-normal: **skew −7.26, kurtosis 147.7** → riesgo
  de cola real que el Sharpe modesto esconde.
- **NO alcanza Sharpe ≥1.0** → no promocionable.

## 5. Estado del sistema (infraestructura)

Implementado y testeado (1228 tests OK): gestión dinámica de TP/SL con ratchet
que **nunca amplía el SL** (`paper_position_plan.py`), motor de riesgo con
kill-switch y pérdida diaria (`paper_risk_state.py`, `configs/risk.yml`),
Telegram con whitelist + doble confirmación + auditoría (`telegram_control.py`),
estado persistente fail-closed con `integrity_sha256`, scanners de seguridad que
fuerzan `live_trading_allowed: false`. Ver checklist completo en
`docs/gates-checklist-2026-07-08.md`.

## 6. Limitaciones conocidas y riesgos abiertos

1. **Sin edge predictivo demostrado** en ETFs diarios/horarios. El único lever de
   edge sin explorar es futuros/forex, cuyos **datos no están integrados** en el
   repo y no pueden inventarse (§8) — requiere que el operador provea/autorice
   una fuente.
2. **Colas gordas** en la estrategia de reglas: el Monte Carlo por resampleo
   puede subestimar drawdowns de días malos consecutivos.
3. **Riesgo/trade Gate 2**: hoy se acota el notional (2%), no el riesgo-a-stop.
4. **Gate 1 (paper validado)** requiere días reales de paper — no cumplido; su
   duración [N días/M trades] está pendiente de confirmar con el operador.
5. **e2e paper supervisado** y **runbook completo** (rollback/Telegram): parciales.
6. Datos horarios obtenidos vía script de sesión read-only con provenance; el
   pipeline gobernado (`fetch-market-data`) sigue siendo diario — expandirlo a
   intradía requiere decisión de gobernanza del operador.

## 7. Próximos pasos (post-D1)

- **Operador:** decidir fuente de datos futuros/forex (único lever de edge
  restante) y duración de Gate 1; confirmar umbrales de riesgo definitivos.
- **Sin datos nuevos:** cerrar e2e paper supervisado, completar runbook, enforcement
  de riesgo-a-stop, y stress de colas (mar-2020/2022 §4).
- **Prohibido** saltar a RL: el goal exige edge de baseline validado primero, y
  hoy no existe.

## 7b. Handover para D2-D5 (continuación por MiniMax u otro ejecutor)

Estado del árbol: rama `live-transition-sprints-impl`, ~40 commits D1, gates
verdes, `models/latest_model.json` intacto. Punto de retoma, en orden:

1. **Desbloqueado ahora (sin operador):**
   - ~~Regime overlay a frecuencia horaria~~ HECHO (evidencia §22): momentum
     intradía es negativo (Sharpe −1.84); el régimen reduce el daño pero no crea
     alfa. Cerrado.
   - Enforcement de **riesgo-a-stop** para Gate 2: la lógica YA existe
     (`build_canary_sizing_decision`, report-only); cablearla en el path de
     ejecución (`paper_position_plan`→`compute_open_notional`) como cap opt-in.
     El umbral `risk_budget_pct` (0.5–1%) es decisión del operador (§10).
   - Stress de colas adicional y walk-forward por-ventana con re-selección de
     params SOLO-pasado (cuidando overfitting; reportar honesto).
2. **Bloqueado en operador (input concreto necesario):**
   - Fuente de datos **futuros/forex** → cerrar placeholders de
     `configs/{futures_micro,forex_major}.yml` (ver `multiasset-config-audit-2026-07-08.md`)
     y replicar la batería de validación.
   - **Duración de Gate 1** [N días / M trades].
   - Autorización de **órdenes paper reales** → arrancar Gate 1 con el ciclo
     `paper-daily --confirm-auto-submit` (hoy corre fail-closed sin submit).
3. **Prohibido:** saltar a RL sin edge de baseline validado; forzar PF≥1.3
   sobreajustando el filtro de régimen; declarar pase de gate que el scorecard
   §20 desmiente.

Artefacto maestro de evidencia: `docs/evidence-2026-07-08-extended-champion-decision.md`
§§1-21. Checklist de estado real: `docs/gates-checklist-2026-07-08.md`.

## 8. Declaración de honestidad

Ninguna métrica de este informe proviene de datos in-sample ni de resultados no
reproducibles. `models/latest_model.json` permanece intacto; no se ejecutó
ninguna promoción ni orden real. El sistema NO está listo para dinero real y esa
decisión, en cualquier caso, es exclusivamente humana.
