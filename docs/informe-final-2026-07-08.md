# Informe honesto de cierre — D1 (2026-07-08)

Entregable DoD §7.7. Este informe reporta **solo** métricas de código ejecutado
en la sesión, con rutas a artefactos. Sin lenguaje de "rentabilidad garantizada"
(prohibido por el goal). Lo que no se pudo demostrar se marca como tal.

## 1. Resumen ejecutivo (una frase honesta)

La **infraestructura** de trading (gestión dinámica de posiciones, motor de
riesgo, kill-switch, Telegram con seguridad, validación estadística) está
mayormente implementada y testeada. El **edge DIRECCIONAL (clasificador ML) NO
existe** en lo accesible. PERO un **filtro de régimen determinista y causal**
sobre la estrategia de reglas (K1) sí produce una mejora del retorno ajustado
por riesgo que **alcanza el gate en agregado y OOS** (Sharpe 0.89 full / 1.07
OOS, DSR 0.96, maxDD 0.80%) — con el caveat honesto de que ese edge es
**dependiente de período** (fuerte 2023-2026, plano/negativo 2021-2023), no una
máquina de dinero constante. La promoción a live sigue siendo decisión humana.

## 1b. Actualización clave (regime filter, evidencia §18-19)

Contrario a la conclusión intermedia de la sesión ("no hay edge"), el filtro de
régimen —hipótesis §3 que probé al final— SÍ da un edge defendible: no es
data-mining (DSR>0 tras deflación de 12 trials, sensibilidad ±20% estable), pero
tampoco all-weather (walk-forward: fold 2021-2023 levemente negativo). Es el
primer resultado del proyecto que cumple el objetivo del goal (retorno ajustado
por riesgo con riesgo acotado y evidencia OOS), con incertidumbre explícita.

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

## 8. Declaración de honestidad

Ninguna métrica de este informe proviene de datos in-sample ni de resultados no
reproducibles. `models/latest_model.json` permanece intacto; no se ejecutó
ninguna promoción ni orden real. El sistema NO está listo para dinero real y esa
decisión, en cualquier caso, es exclusivamente humana.
