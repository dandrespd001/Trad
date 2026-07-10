# Checklist de gates — estado real (2026-07-08, D1)

Entregable DoD §7.6. Estado auditado contra el código y los artefactos de esta
sesión. Leyenda: ✅ cumplido y verificado · ◑ parcial/implementado pero no
cerrado · ⛔ pendiente/bloqueado. Ninguna afirmación sin evidencia (regla §8).

Suite: **1228 tests OK** (skipped=4), `verify-release-minimal.sh` PASS,
scanners live/futures limpios. `models/latest_model.json` intacto; cero
promociones esta sesión.

## A. Entrenamiento y validación (§3–§4)

| Ítem | Estado | Evidencia |
| --- | --- | --- |
| Split temporal + embargo (purga López de Prado) | ✅ | `temporal_train_test_split(embargo=)`, walk-forward por-ventana; embargo=horizonte en triple-barrier (H2, 875bda3) |
| Checklist anti-leakage (features t-only, norm train-only) | ✅ | estandarización train-only (H1, afb3cff); stats por ventana; xs by-date (I2, 8eb159f) |
| Baseline simple con edge OOS **antes** de complejizar | ✅ (regla respetada) | logístico → LightGBM; RL NO tocado por falta de edge |
| Datasets versionados (hash) | ✅ | `dataset_hash` en run artifacts; provenance+sha256 del hourly (07ebab3) |
| Multiple testing → PSR/DSR | ✅ | `probabilistic_sharpe_ratio`/`deflated_sharpe_ratio` (J1, db5ecc0) |
| Monte Carlo (DD p95) | ✅ | `monte_carlo_drawdown`; aplicado (b96d4aa): p95 2.96% |
| Sensibilidad ±20% | ✅ | §13 evidencia (f22b419): maxDD ~1% robusto |
| **Edge OOS demostrado (batería §4 completa)** | ✅ **6/6 con umbrales confirmados por el operador** | risk-parity ETF+cripto con vol-target: Sharpe 1.088 full/1.380 OOS, MaxDD 3.8%, MC p95 6.6%, DSR 0.995, PF 1.205, 7/9 años+, walk-forward todos+. **Operador aceptó cripto Y PF ~1.2 (2026-07-09)** → batería completa con umbrales confirmados. A costo Alpaca realista 35bps aguanta (1.017/1.282, §30) con margen fino. Adaptación de parámetros probada y RECHAZADA (§31) — la adaptación queda a nivel riesgo (vol-target+risk-parity, ya operando). Caveat: ETF 1bp. (Régimen §23 y ML direccional refutados) |

## B. Gestión dinámica de posiciones (§5)

| Ítem | Estado | Evidencia |
| --- | --- | --- |
| Trailing por ATR | ✅ | `paper_position_plan.py` `trailing_stop` |
| Break-even tras trigger (ratchet) | ✅ | `breakeven_stop`, high-water mark monotónico |
| **Nunca ampliar el SL** | ✅ | ratchet vía `max(entry, price, trailing_high)` — solo aprieta |
| Tomas parciales | ◑ | position plan soporta niveles; e2e por confirmar |
| Sizing por riesgo | ✅ | `execution/position_sizing.py` |

## C. Motor de riesgo y kill-switch (§5)

| Ítem | Estado | Evidencia |
| --- | --- | --- |
| Pérdida diaria máx → pausa | ✅ | `configs/risk.yml` `max_daily_loss_pct: 0.02`; `paper_risk_state.py` |
| Kill-switch por drawdown | ✅ | `max_drawdown_pct: 0.10`; latch por racha de errores |
| Topes de posición/exposición | ✅ | `max_single_position: 0.02`, `max_gross_exposure: 1.0` |
| Riesgo/trade ≤0.5–1% (equity a stop) | ⛔ **pendiente Gate 2** | el sizing de EJECUCIÓN (`paper_position_plan`→`compute_open_notional`) acota *notional* (2%), no riesgo-a-stop. La LÓGICA riesgo-a-stop YA existe (`build_canary_sizing_decision`: notional ≤ bankroll·risk_budget/stop) pero es report-only, no cableada a ejecución. Gate 2 = reusarla + fijar `risk_budget_pct` (umbral §10, decisión operador) |
| Estado persistente fail-closed (integrity_sha256) | ✅ | `paper_risk_state.py` idioma checksum |

## D. Robustez operativa (§5)

| Ítem | Estado | Evidencia |
| --- | --- | --- |
| Reconciliación con broker | ◑ | `execution/live_reconciliation.py` (código); e2e por confirmar |
| Reintentos idempotentes / recuperación tras reinicio | ◑ | estado persistente + ledgers append-only |
| Watchdog + heartbeat | ◑ | timer systemd `trading-n0.timer`; heartbeat por cerrar |

## E. Telegram (§6)

| Ítem | Estado | Evidencia |
| --- | --- | --- |
| Whitelist de chat_id | ✅ | `telegram_control.py` rechaza chat_id no permitido |
| Doble confirmación en acciones destructivas | ✅ | `requires_confirmation` + `confirm_telegram_control` |
| Token en env (nunca en repo/logs) | ✅ | `~/.config/trading-ai/paper.env`; redacción recursiva (E1-E2) |
| Log de auditoría de comandos | ✅ | inbox auditado + ledger |
| Set completo de comandos + alertas push | ◑ | status/history/send/control presentes; cobertura total por confirmar |

## F. Gates de ejecución live (§5)

| Gate | Estado | Nota |
| --- | --- | --- |
| Gate 0 — paper por defecto, live off | ✅ | `live_trading_allowed: false`; scanner lo fuerza |
| Gate 1 — [N] días/[M] trades paper validados | ◑ **EN MARCHA (portafolio completo §28)** | órdenes paper reales funcionan en equity (K3) y CRIPTO (M2: $10 BTC/USD FILLED gtc, 2026-07-09). Timer `trading-crypto-sleeve.timer` APROBADO e instalado (diario 19:05): fetch gobernado → `sleeve-allocate` (M4, risk-parity, cap 1.0) → `sleeve-rebalance` cripto + ETF, $1000 totales. Día 1 (2026-07-09): cripto FLAT honesto (momentum 120d negativo los 6 pares); ETF 3 buys $50 (IWM/XLF/XLV) ACCEPTED para la apertura; re-run mismo día → duplicados reportados WARN (idempotencia verificada). Faltan los días reales (<16, no comprimibles) |
| Gate 2 — canario (confirmación humana) | ⛔ pendiente | gates implementados; promoción es decisión humana |

## G. Entregables DoD (§7)

| # | Entregable | Estado |
| --- | --- | --- |
| 1 | Auditoría inicial + backlog | ✅ (auditorías en `docs/audit-*`) |
| 2 | Código con tests pasando | ✅ (1228 OK) |
| 3 | Reporte de validación (CV+WF+DSR+MC) | ✅ (`docs/evidence-2026-07-08-*` §§1-15) |
| 4 | Paper e2e supervisado sin errores críticos | ✅ **FILL REAL** 2026-07-09 (K3 e611551): $1 XLV señal→preflight→gates→price-sanity→submit→FILL @162.18→close. Ciclo completo contra broker paper real. Antes bloqueado por 3 bugs (feed SIP, market_data no cableado, reference_price ausente) |
| 5 | Runbook operativo | ✅ (`docs/paper-real-runbook.md` + `n0-campaign-runbook.md` + `runbook-recovery-rollback.md` arranque/parada/recuperación/rollback/Telegram) |
| 6 | Checklist de gates | ✅ (este documento) |
| 7 | Informe final honesto | ✅ (`docs/informe-final-2026-07-08.md`) |

## Conclusión honesta

La **infraestructura** (gestión dinámica, riesgo, kill-switch, Telegram con
seguridad, validación estadística) está mayormente implementada y testeada. El
**edge** NO está: ningún modelo/estrategia accesible alcanza el gate Sharpe ≥1.0
OOS. Por tanto el sistema NO es promocionable a live — y esa decisión es humana
en todo caso. Próximos pasos reales: (a) fuente de datos futuros/forex
(operador) para el único lever de edge sin explorar; (b) cerrar los ◑ (e2e
paper supervisado, runbook completo, informe final); (c) enforcement de
riesgo-a-stop para Gate 2.
