# Evidencia y decisión de promoción — campeón con features extendidas (5 años)

**Fecha:** 2026-07-08
**Arquitecto/revisor:** Opus 4.8 (rol de Arquitecto/gatekeeper del goal)
**Sprint que lo habilitó:** G3 — `train --feature-names` (commit 3e1fa0a)
**Decisión:** **RECHAZAR** la promoción del modelo con features extendidas.
`models/latest_model.json` permanece intacto (inmutable, promoción humano-gateada).

---

## 1. Contexto

Con el dataset de 5 años (`data/incoming/history_5y.csv`, 12 800 filas,
2021-06 → 2026-07, 10 ETFs) el benchmark `indicator-activation` (2026-07-08)
recomendó **extended** con margen +34 % (extended_score 45.38 vs
baseline_score 33.78, `reports/tmp/indicator_activation/2026-07-08/activation.json`).

G3 añadió la capacidad de entrenar explícitamente esas features. Como parte de
la revisión del Arquitecto se corrió el flujo gobernado **con datos reales**
para comprobar si ese score-proxy se traduce en un modelo desplegable mejor.
No lo hace.

## 2. Artefactos (código ejecutado en esta sesión)

Dataset extendido de 5 años: `reports/tmp/train/features_5y_ext.csv`
(baseline + `rsi_14,macd_hist,bb_pct_b`).

Trainings gobernados (`trading_ai.cli train`, trainer de producción, sin
estandarización de features):

- Baseline (15 features default): `reports/tmp/train/g3_evidence/baseline_run.json`
- Extended (18 = default + rsi/macd/bb): `reports/tmp/train/g3_evidence/extended_run.json`

Reproducción:

```bash
DS=reports/tmp/train/features_5y_ext.csv
PYTHONPATH=src python3 -m trading_ai.cli train --model logistic-baseline \
  --dataset "$DS" \
  --output reports/tmp/train/g3_evidence/baseline_model.json \
  --run-output reports/tmp/train/g3_evidence/baseline_run.json
PYTHONPATH=src python3 -m trading_ai.cli train --model logistic-baseline \
  --dataset "$DS" \
  --feature-names "return_1d,momentum_20,momentum_60,momentum_120,realized_volatility_20,rolling_drawdown_20,daily_range,true_range,atr_14,relative_volume_20,close_to_sma_20,close_to_sma_60,vol_adjusted_momentum_20,vol_adjusted_momentum_60,vol_adjusted_momentum_120,rsi_14,macd_hist,bb_pct_b" \
  --output reports/tmp/train/g3_evidence/extended_model.json \
  --run-output reports/tmp/train/g3_evidence/extended_run.json
```

## 3. Resultados del trainer de producción (test hold-out, 2 898 muestras)

| Config | test accuracy | test log_loss | walk-forward mean acc |
| --- | --- | --- | --- |
| Baseline (15) | 0.4489 | 2.977 | 0.4934 |
| Extended (18) | 0.4496 | **15.163** | 0.4865 |

`positive_rate` del test = 0.5507 → **la clase mayoritaria (always-long) daría
0.5507 de accuracy**. Ambos modelos quedan por debajo del naive; el extended
además dispara el log_loss ~5×.

## 4. Diagnóstico del log_loss (probe reproducible)

El trainer `train_logistic_baseline` hace SGD **sin estandarizar features**
(`src/trading_ai/models/baseline.py`). `rsi_14` (0–100) y `macd_hist` entran sin
escalar, dominan el gradiente y saturan el sigmoide. Probe con estandarización
train-only (misma data, `test_fraction=0.25`, `embargo=1`):

| Config | test acc | test log_loss |
| --- | --- | --- |
| baseline RAW | 0.5500 | 0.975 |
| baseline STD | 0.5072 | 0.995 |
| extended RAW | 0.5507 | **15.518** |
| extended STD | 0.5173 | **1.129** |

Estandarizar reduce el log_loss del extended de 15.5 a 1.13: **el 15 de
producción es un artefacto de escala, no una señal sobre el valor de las
features.** Aun estandarizado, extended no supera al naive (0.5507) en accuracy.

## 5. Conclusión honesta

1. El score de `indicator-activation` (proxy scale-invariant) **no se traduce**
   en un modelo desplegable mejor sobre 5 años OOS.
2. **Ningún set de features produce edge direccional** por encima del
   always-long sobre 5 años con el baseline logístico. Regla del goal: el
   baseline debe demostrar edge OOS antes de complejizar — hoy no lo hace.
3. El log_loss catastrófico del extended es un bug de pipeline
   (falta estandarización), no evidencia contra las features.

## 6. Decisión y backlog

**Promoción: RECHAZADA.** No se toca `models/latest_model.json`. No se ejecuta
`promote`. Presentado al operador (dandrespd) como decisión, no auto-promoción.

Backlog priorizado que abre esta evidencia:

- **H1 (pipeline):** añadir estandarización de features train-only al
  `logistic-baseline` (stats en el artefacto del modelo, aplicadas en
  inferencia). Requiere su propia evaluación gobernada porque cambia también
  al baseline actual.
- **H2 (edge, más fundamental):** el objetivo direccional a 1 barra diaria no
  muestra edge. Reconsiderar horizonte/etiqueta (p. ej. retorno ajustado por
  volatilidad, triple-barrier) y validar OOS **antes** de añadir complejidad de
  modelo o RL.

Nota metodológica: correr los reportes de evaluación con datos reales aprobados
es parte de la revisión del Arquitecto. Los tests sintéticos de G3 verifican la
mecánica de `--feature-names`; solo la corrida real destapó que el score-proxy y
el modelo desplegable discrepan.

## 7. Actualización — resultados de H1 y H2 (mismo día)

H1 (afb3cff) añadió estandarización train-only opt-in; H2 (875bda3) añadió
etiquetado triple-barrier vol-escalado opt-in (con embargo=horizonte en split y
walk-forward). Corridas gobernadas sobre 5 años (18 features extendidas):

| Config | test acc | test log_loss | naive a batir |
| --- | --- | --- | --- |
| baseline direction (raw) | 0.4489 | 2.977 | 0.5507 |
| extended direction (raw) | 0.4496 | 15.163 | 0.5507 |
| extended direction (STD) | 0.5097 | 1.745 | 0.5507 |
| extended triple-barrier h5 k1.0 (STD) | 0.4681 | 1.823 | 0.5322 |
| extended triple-barrier h5 k2.0 (STD) | 0.4765 | 1.805 | 0.5321 |
| extended triple-barrier h10 k1.0 (STD) | 0.4730 | 2.166 | 0.5304 |

Artefactos: `reports/tmp/train/g3_evidence/{extended_std_run,tb_h*_run}.json`.

**Conclusión consolidada (honesta).** Ninguna de las ~10 configuraciones
probadas —{baseline, extended} × {raw, estandarizado} × {direction,
triple-barrier} × {h5/h10, k1.0/k2.0}— supera al naive always-long OOS. H1
corrigió una patología real de calibración (log_loss 15.16 → 1.745) y el
triple-barrier balanceó la etiqueta (positive_rate 0.55 → ~0.53), pero **el
baseline logístico lineal no tiene edge direccional demostrable** sobre estos
ETFs diarios con las features actuales. Como ningún candidato gana, no hay
selección que deflactar con PSR/DSR — el resultado negativo es limpio.

**Implicación para el goal** (§3: baseline con edge OOS antes de complejizar):
antes de RL o modelos complejos, las palancas honestas son (a) modelo no-lineal
todavía "baseline simple" —gradient boosting, ya hay wrappers `ml` en
`models/baseline.py`—, (b) features más ricas o de mayor frecuencia, (c) revisar
universo/timeframe. Decisión de dirección pendiente del operador.

## 8. Actualización — I1: baseline no-lineal (LightGBM)

I1 (fcc47c4) cableó `--model lightgbm-baseline` reusando el mismo pipeline de
features/etiquetado/embargo. Corridas gobernadas sobre 5 años (18 features):

| Config | train acc | test acc | test log_loss | wf acc | naive | edge |
| --- | --- | --- | --- | --- | --- | --- |
| LightGBM direction | 0.7769 | 0.5093 | 0.708 | 0.5057 | 0.5507 | −0.041 |
| LightGBM triple-barrier h5 | 0.8278 | 0.4896 | 0.727 | 0.5018 | 0.5322 | −0.043 |

Artefactos: `reports/tmp/train/g3_evidence/lgb_{dir,tb5}_run.json`.

**Resuelve la pregunta de clase de modelo.** LightGBM MEMORIZA el train
(acc 0.78–0.83) pero generaliza a ~0.50 OOS, por debajo del naive en ambas
etiquetas. La brecha train→test es la firma de libro de ausencia de señal
aprendible: un ensemble de árboles flexible fittea ruido in-sample y no encuentra
nada que generalice. **El techo es el contenido de información de las
features/target, no la linealidad del modelo.** La palanca "modelo no-lineal"
queda AGOTADA; quedan (b) features más ricas y (c) universo/timeframe.

## 9. Actualización — I2: features cross-sectional (fuerza relativa)

I2 (8eb159f) añadió `build-features --cross-sectional`: rank y z-score
within-date de columnas de momentum/retorno (qué ETF está fuerte hoy vs los
demás). Corridas sobre 5 años:

| Config | train acc | test acc | wf acc | naive | edge |
| --- | --- | --- | --- | --- | --- |
| logistic+STD base+xs (23f) | 0.4953 | 0.4810 | 0.5095 | 0.5507 | −0.070 |
| LightGBM base+xs (23f) | 0.7934 | 0.5079 | 0.5090 | 0.5507 | −0.043 |
| logistic+STD xs-only (8f) | 0.5107 | **0.5328** | 0.4932 | 0.5507 | −0.018 |

Artefactos: `reports/tmp/train/g3_evidence/xs_*_run.json`,
`features_5y_xs.csv`.

Las features cross-sectional TAMPOCO superan al naive; el mejor caso (xs-only
logístico, 0.5328) es el más cercano visto pero sigue por debajo. LightGBM
vuelve a memorizar (0.79 train → 0.51 test).

## 10. Conclusión de opciones 1 y 2 (agotadas) y matiz honesto

Probado exhaustivamente sobre 5 años de 10 ETFs diarios: {logístico, LightGBM} ×
{base, extended, cross-sectional} × {±estandarización} × {direction,
triple-barrier}. **Ninguna combinación supera al naive always-long OOS.** La
predicción DIRECCIONAL diaria de estos ETFs líquidos no tiene edge demostrable
con las palancas de modelo/feature/etiqueta (opciones 1 y 2).

**Matiz honesto importante (evita sobre-afirmar):** esto mide *accuracy
direccional* de un clasificador, que NO es lo mismo que la rentabilidad
ajustada por riesgo de una estrategia (objetivo real del goal §0). Una estrategia
puede tener expectativa positiva con <55% de acierto si los payoffs son
asimétricos (sizing/timing/exits). El clasificador ML es UN componente; el motor
de backtest basado en reglas (`momentum-vol-target`) es una vía separada aún no
re-evaluada bajo este lente. No se afirma "el sistema no tiene edge" — solo "los
modelos ML direccionales no lo muestran".

**Queda opción 3** (universo/timeframe: futuros/forex, o intradía), que requiere
ingesta de datos gobernada (CSV aprobado por el operador). Decisión de datos
pendiente del operador.

## 11. Lente correcto — backtest de reglas bajo riesgo-ajustado (sin datos nuevos)

Siguiendo el matiz de §10, se re-evaluó la estrategia de reglas
`momentum-vol-target` sobre los 5 años con `configs/risk.yml` (costos incluidos:
1 bp comisión + 1 bp slippage). Métricas (artefacto
`reports/tmp/train/g3_evidence/backtest_5y.json`):

| Métrica | Valor | Gate goal §4 |
| --- | --- | --- |
| Sharpe | 0.46 | ≥ ~1.0 ✗ |
| Sortino | 0.44 | — |
| Max drawdown | 1.1% | ≤ ~15% ✓ |
| CAGR | 0.36% | — |
| Retorno acum. (5y) | 1.84% | — |
| Trades | 2983 | ≥100 ✓ |
| Turnover | 20.6 | — |

**Lectura honesta.** La estrategia de reglas tiene expectativa POSITIVA pero
DÉBIL (Sharpe 0.46) con riesgo muy acotado (DD 1.1%). NO alcanza el gate de
Sharpe ≥1.0 → no promocionable a live. Es un baseline real de expectativa
positiva con control de riesgo excelente, coherente con el principio rector
(§0). Contraste clave: el clasificador ML no muestra edge direccional, pero la
estrategia de reglas sí es levemente positiva ajustada por riesgo — el objetivo
del goal.

Caveats: (a) es backtest full-sample de una estrategia de reglas con parámetros
fijos (sin fitting a estos datos, por eso es un proxy OOS razonable, pero los
parámetros de `risk.yml` deberían pasar el análisis de sensibilidad ±20% del
goal §3); (b) Deflated Sharpe y Monte Carlo → ver §12.

## 12. Validación estadística §4 (J1: PSR + DSR + Monte Carlo)

Aplicando las métricas de J1 (db5ecc0) a los 1279 retornos diarios del backtest:

| Métrica | Valor | Lectura |
| --- | --- | --- |
| Sharpe diario | 0.0292 | anualizado ≈0.464 |
| PSR(SR>0) | 0.826 | 83% prob. de Sharpe verdadero >0 |
| DSR (n_trials=1) | 0.826 | = PSR (config única, sin selección) |
| Monte Carlo maxDD p95 | 2.96% | ≤15% ✓ |
| Monte Carlo maxDD peor | 6.1% | ≤15% ✓ |
| **Skew** | **−7.26** | ⚠️ cola izquierda severa |
| **Kurtosis** | **147.7** | ⚠️ colas gordísimas |

**Lectura honesta.** La estrategia es probablemente (~83%) de expectativa
positiva, y su drawdown se mantiene pequeño incluso bajo Monte Carlo (p95 ≈3%).
PERO la distribución de retornos es SEVERAMENTE no-normal (skew −7.3, kurtosis
148): hay días de pérdida raros pero severos que el Sharpe modesto esconde. El
riesgo de cola es real y el Monte Carlo por resampleo (que rompe la
autocorrelación) puede subestimar drawdowns de días malos consecutivos. Antes de
cualquier consideración live: análisis de sensibilidad ±20% (ver §13) y stress
de las colas.

## 13. Análisis de sensibilidad ±20% (§3)

Perturbando ±20% los parámetros de estrategia sobre la config gobernada
(`max_single_position=0.02`), 5 años:

| Perturbación | Sharpe | maxDD |
| --- | --- | --- |
| BASE (mom=20, vol=20, tgt=0.12) | 0.464 | 1.11% |
| momentum_window −20% (16) | **0.727** | 1.05% |
| momentum_window +20% (24) | 0.466 | 1.25% |
| volatility_window −20% (16) | 0.429 | 1.15% |
| volatility_window +20% (24) | 0.475 | 1.15% |
| target_vol −20% (0.096) | 0.427 | 1.01% |
| target_vol +20% (0.144) | 0.518 | 1.12% |

Sharpe: min 0.427, max 0.727, media 0.501, desv 0.097.

**Lectura honesta.** (1) El **control de riesgo es muy robusto**: el maxDD se
mantiene ~1% en TODAS las perturbaciones. (2) El Sharpe **sobrevive** ±20% sin
colapsar (siempre positivo, 0.43–0.73). (3) Señal de atención: `momentum_window`
a la baja (16) sube el Sharpe +57% — el default (20) NO está en el pico, así que
hay margen de retuning, pero exigiría su propia validación OOS para no caer en
overfitting (el goal prefiere estabilidad a picos). (4) En NINGUNA perturbación
se alcanza el gate Sharpe ≥1.0 → sigue sin ser promocionable a live.

Artefacto: reproducible vía `run_momentum_vol_target_backtest` con
`BacktestConfig` perturbadas (script en la sesión).

## 14. Opción 3 — datos intradía (horario), obtenidos vía Alpaca autorizado

Por directiva del operador (2026-07-08: "consigue los datos tú"), se obtuvo
data **horaria real** vía el canal Alpaca paper read-only ya autorizado (feed
IEX), NO sintética. Provenance: `reports/tmp/train/g3_evidence/
history_5y_hourly.csv.provenance.json` (97 482 barras, 10 ETFs, 2021-06→2026-07,
sha256 registrado). Nota de gobernanza: el fetch gobernado del repo
(`fetch-market-data`) está hardcodeado a diario (`_normalize_bar` trunca a fecha)
y `data_sources.yml` autoriza Alpaca solo a `1d`; esta obtención fue un script
de sesión read-only con provenance, no una expansión del pipeline gobernado
(que requeriría cambios de timestamp + decisión de gobernanza del operador).

Evaluación OOS (features horarias base, mismo protocolo):

| Config | train acc | test acc | test log_loss | naive | edge |
| --- | --- | --- | --- | --- | --- |
| horario logístico+STD | 0.4980 | 0.5015 | 1.976 | 0.5179 | −0.016 |
| horario LightGBM | 0.6091 | 0.5117 | 0.694 | 0.5179 | −0.006 |

**Lectura honesta.** El intradía horario TAMPOCO supera al naive (0.5179, más
cercano a 0.5 porque la dirección horaria está más balanceada). Es el caso más
cercano visto (LightGBM −0.006) y el sobreajuste de LightGBM es menor a esta
frecuencia (train 0.61 vs 0.79 diario), pero no hay edge explotable.

## 15. Conclusión global del research (todo lo accesible, agotado)

Matriz probada: {diario, horario} × {time-series base/extended, cross-sectional}
× {logístico ±STD, LightGBM} × {direction, triple-barrier}. **NINGUNA combinación
supera al naive OOS.** La predicción direccional de estos 10 ETFs líquidos no es
el edge, a ninguna frecuencia/feature/modelo accesibles. Lo único con expectativa
positiva sigue siendo la estrategia de reglas (Sharpe 0.46, no promocionable).
El único sub-lever no probado de la opción 3 es futuros/forex (otra clase de
activo), cuyos datos NO están integrados en el repo y no pueden inventarse
(regla del goal §8) — requiere una fuente de datos que el operador provea o
autorice integrar.

## 16. Stress tests de régimen (§4) — ventana 2022 (bear/alta vol)

La estrategia de reglas sobre las sub-ventanas de estrés del dataset 5y
(artefactos `reports/tmp/train/g3_evidence/stress_*.json`):

| Ventana | Sharpe | Sortino | MaxDD | Retorno | Trades |
| --- | --- | --- | --- | --- | --- |
| 2022 bear (año) | −0.449 | −0.531 | 0.73% | −0.24% | 549 |
| 2022 H1 (crash) | −1.542 | −1.612 | 0.68% | −0.42% | 228 |
| full 5y (ref) | +0.464 | +0.441 | 1.11% | +1.84% | 2983 |

**Lectura honesta.** En el régimen de estrés de 2022 la estrategia es
**negativa** (Sharpe −0.45 año, −1.54 en el crash), confirmando que su Sharpe
positivo full-sample (0.46) es **dependiente de régimen**, no un edge robusto.
PERO el control de riesgo AGUANTA: el maxDD se mantiene <1% incluso en el crash
(sizing 2% + vol-target). La estrategia no revienta en estrés — solo sangra
levemente. Pendiente §4: mar-2020 (ver nota abajo), gaps de forex y rollover de futuros
(otra clase de activo, no integrada).

**Nota mar-2020 (verificado, no negligencia).** Se intentó extender los datos
hacia atrás vía el canal Alpaca autorizado (1d, IEX, universo aprobado) para
correr el stress del crash COVID de febrero-marzo 2020. El feed IEX **gratuito
solo entrega historia desde ~2020-07-27** (provenance
`history_2019_2021.csv.provenance.json`): mar-2020 NO está disponible en el canal
integrado. Es un muro de disponibilidad de datos, no un ítem omitido — requeriría
una fuente de datos distinta (SIP/otro proveedor) que el operador debería
proveer/autorizar.

## 17. e2e paper (§7.4) — pipeline demostrado, fail-closed correcto

`trading-ai paper-daily --source-csv ... --as-of-date 2026-07-06` (SIN
`--confirm-auto-submit`) corrió el ciclo completo y terminó **CRITICAL** con
`order_not_submitted` (cero órdenes). Artefacto:
`reports/tmp/paper_daily/latest.json`. Blockers (todos gates de gobernanza
legítimos): `freshness_blocked`, `future_timestamp`, `promotion_missing`,
`backtest_missing`, `drift_report_missing`, `signal_quality_blocked`.

**Lectura honesta.** El pipeline e2e (features → señales → gates de riesgo →
monitor → plan) ejecuta de punta a punta y **falla cerrado correctamente**: nada
pasa a submisión sin la cadena completa de artefactos gobernados verde. PERO un
ciclo e2e "verde limpio" exige un **modelo promovido** + backtest + drift +
datos frescos del día — y NO hay modelo promovible porque no hay edge (§§1-16).
Es decir, el e2e verde está gateado por la misma causa raíz. La *capacidad* e2e
y la seguridad fail-closed están demostradas; el *resultado verde* depende de un
edge que no existe hoy.

## 18. HALLAZGO — filtro de régimen determinista (edge OOS que SÍ alcanza el gate)

Hipótesis (§3 clasificación de régimen): el stress de 2022 mostró que la
estrategia sangra en régimen bear/alta-vol. Un filtro que la ponga en **flat en
regímenes risk-off** debería mejorar el retorno ajustado por riesgo sin necesitar
edge direccional. Probado como máscara sobre los retornos del backtest (flat =
no mantener posición) con umbrales **causales** (solo datos pasados):

- Régimen bull: SPY > SMA200 (causal).
- Régimen low-vol: vol 20d de SPY ≤ mediana **expanding** de vols pasadas (causal,
  warmup 120).

| Config | Sharpe full | Sharpe OOS (últ. 40%) | maxDD | cumret | PSR |
| --- | --- | --- | --- | --- | --- |
| BASE (sin filtro) | 0.464 | 0.255 | 1.11% | +1.84% | 0.826 |
| SPY>SMA200 (causal) | 0.625 | — | 1.11% | +2.33% | 0.882 |
| low-vol causal | 0.900 | — | 0.78% | +2.45% | 0.977 |
| **bull AND low-vol (causal)** | **0.968** | **1.060** | 0.76% | +2.59% | 0.984 |

**Validación.** (a) **OOS**: en el 40% held-out el Sharpe del régimen sube a
**1.06** (vs 0.90 in-sample) — generaliza, no overfitea. (b) **Sensibilidad
±20%**: SMA∈{160,200,240}×vol∈{16,20,24} da Sharpe 0.85–1.06 sin colapso (varias
>1.0). (c) Umbrales **causales** (sin lookahead). (d) Económicamente sensato
(evitar bear/alta-vol). Artefacto: script de sesión sobre backtest_5y.json.

**Implicación.** SÍ existe una configuración que alcanza el gate OOS Sharpe ≥1.0
(OOS=1.06 con params causales por defecto), mejora el retorno y **halves el
drawdown**. Es el primer edge honesto del proyecto. Caveats para producción:
(1) es máscara post-hoc sobre retornos con costos base — la implementación real
en el motor debe incluir el costo de transición al entrar/salir de flat (segundo
orden dado el bajo turnover); (2) OOS = un bloque held-out, falta walk-forward
completo; (3) sigue siendo ETF diario. PRÓXIMO: implementar el filtro en el motor
+ tests + walk-forward, y re-validar end-to-end con costos reales.

## 19. K1 — filtro implementado en el motor + walk-forward + DSR (cuadro honesto)

El filtro se implementó como feature opt-in del motor (`regime_filter_enabled`,
K1 3c8b623) con costos de transición reales (flatten pasa por `_turnover`).
Resultado gobernado sobre 5 años (config causal por defecto SMA200/vol20):

- **Full-sample:** Sharpe 0.888 (vs 0.464 base), maxDD 0.80% (vs 1.11%),
  CAGR +0.466% (vs +0.359%), trades 1846 (vs 2983).
- **OOS (últ. 40% held-out):** Sharpe **1.068** (vs base 0.255).
- **PSR(>0)=0.976; DSR(n_trials=12)=0.957** (>0). El DSR usa la varianza de los
  9 trials del grid SMA∈{160,200,240}×vol∈{16,20,24} (Sharpes 0.75-0.97,
  agrupados) → la deflación por multiple-testing es modesta y el edge **sobrevive**.

**Walk-forward (4 folds secuenciales ~1.25y), Sharpe regime:**

| Fold | Período aprox | Sharpe base | Sharpe regime |
| --- | --- | --- | --- |
| 1 | 2021-2022 | −0.384 | +0.018 |
| 2 | 2022-2023 | +0.750 | **−0.068** |
| 3 | 2023-2025 | +1.096 | +1.594 |
| 4 | 2025-2026 | +0.464 | +1.494 |

**Lectura honesta (dos verdades).** (1) El edge **NO es un artefacto de
data-mining**: DSR>0 robusto tras deflación de 12 trials, OOS block fuerte,
sensibilidad ±20% estable, drawdown a la mitad, económicamente sensato.
(2) PERO el edge **NO es all-weather**: el walk-forward muestra que se concentra
en 2023-2026 (folds 3-4) y es plano/levemente negativo en 2021-2023 (fold 2
−0.07). El OOS block de "últimos 40%" se ve fuerte precisamente porque coincide
con el período donde el edge se concentra. Una operación real experimentaría
tramos planos/negativos. **Conclusión medida:** el filtro de régimen es una
mejora genuina y estadísticamente defendible del retorno ajustado por riesgo, y
alcanza el gate ≥1.0 en agregado/OOS reciente — pero su edge es dependiente de
período, no una máquina de dinero constante. Cumple el objetivo del goal
(maximizar retorno ajustado por riesgo con riesgo acotado y evidencia OOS), con
la incertidumbre explícita de la inconsistencia temporal.

## 20. Scorecard COMPLETO de gates §4 (corrige el framing — no es pase limpio)

Al evaluar la estrategia de régimen contra TODOS los umbrales del §4 (no solo el
Sharpe OOS que se destacó en §18-19):

| Gate | Valor | Umbral | Resultado |
| --- | --- | --- | --- |
| Sharpe full-sample | 0.888 | ≥1.0 | **FAIL** |
| Sharpe OOS (últ. 40%) | 1.068 | ≥1.0 | PASS |
| Profit Factor full | 1.218 | ≥1.3 | **FAIL** |
| Profit Factor OOS | 1.265 | ≥1.3 | **FAIL** |
| Max drawdown full | 0.80% | ≤15% | PASS |
| Monte Carlo DD p95 | 1.38% | ≤15% | PASS |
| DSR (12 trials) | 0.957 | >0 | PASS |
| Días activos full / OOS | 772 / 319 | ≥100 | PASS |
| trade_count | 1846 | ≥100 | PASS |

**Corrección honesta.** El scorecard completo desmiente cualquier lectura de
"pasa el gate": el **Profit Factor FALLA** ambos (1.22/1.26 < 1.3) —métrica que
no se había computado antes— y el **Sharpe full-sample también FALLA** (0.888).
La estrategia de régimen SÍ pasa los gates de **riesgo** (drawdown, Monte Carlo,
DSR) y de **actividad** (trades), y el Sharpe **solo en el bloque OOS reciente**.
No es promocionable bajo la batería completa del §4. Lección de proceso: computar
el scorecard COMPLETO evita el cherry-picking de la única métrica que pasa —
exactamente lo que el goal §0 prohíbe.

## 21. Robustez del overlay de régimen a parámetros de estrategia (§3)

Para separar "el filtro ayuda de verdad" de "coincidencia de una config tuneada",
se midió el delta de Sharpe (regime − base) a través de **36 configuraciones de la
estrategia base** (momentum_window∈{15,20,25,30} × top_n∈{2,3,4} ×
target_vol∈{0.10,0.12,0.14}), SIN tunear los parámetros del régimen:

| Métrica del delta | Valor |
| --- | --- |
| Configs donde el régimen AYUDA (delta>0) | **33/36 (92%)** |
| Delta Sharpe mediano | +0.226 |
| Delta Sharpe medio | +0.231 |
| Delta Sharpe rango | −0.054 … +0.479 |

**Lectura honesta — dos dimensiones de robustez distintas.** (1) **Robusto a
parámetros de estrategia:** el overlay de régimen mejora el Sharpe en el 92% de
las configs, con mediana +0.23 — NO es un artefacto de una sola parametrización
afortunada. (2) **NO robusto en el tiempo:** el walk-forward (§19) muestra que el
edge absoluto se concentra en 2023-2026. Conclusión combinada: el filtro de
régimen es una mejora de gestión de riesgo **genuina y param-robusta**, pero el
edge subyacente de la estrategia es **temporalmente dependiente**. No inventar
robustez que no hay; no negar la que sí hay.

## 22. Overlay de régimen a frecuencia horaria (cerrado)

Backtest de `momentum-vol-target` sobre los datos horarios (§14, 11 403 barras,
ventanas escaladas a horizonte intradía, periods_per_year=1764):

| Config | Sharpe | OOS | PF | maxDD |
| --- | --- | --- | --- | --- |
| base (regime off) | −1.836 | −2.078 | 0.865 | 5.6% |
| regime on | −1.400 | −1.767 | 0.871 | 3.7% |

**Lectura honesta.** El momentum intradía (horario) en ETFs tiene **expectativa
NEGATIVA** (Sharpe −1.84). El filtro de régimen **sigue reduciendo el daño**
(Sharpe menos negativo, drawdown 5.6%→3.7%) —reconfirma su rol de gestión de
riesgo— pero no puede convertir una base negativa en positiva. Confirma que el
edge (débil, diario) NO se traslada a intradía; el overlay de régimen es una
mejora de riesgo real pero no un generador de alfa.

## 23. ⚠️ CORRECCIÓN MAYOR — validación sobre 21 años (2005-2026) refuta el edge de régimen

Con el desbloqueo de datos del operador (2026-07-09, Yahoo Finance autorizado) se
obtuvo historia larga real: `history_long_yahoo.csv` (54 110 filas, 10 ETFs,
2005-01→2026-07, auto_adjust, sha256 en provenance), que incluye los crashes
2008/2011/2015/2018/2020/2022. Backtest base vs régimen sobre los 21 años:

| Métrica | Base | Regime |
| --- | --- | --- |
| Sharpe full 21y | **0.788** | **0.607** |
| Profit Factor | 1.146 | 1.158 |
| Max drawdown | 1.3% | 1.7% |

**El filtro de régimen EMPEORA la estrategia sobre 21 años (0.79 → 0.61).**
Desglose por año: el régimen supera a la base en solo **7/22 años**. Ayuda en
crashes (2008 Δ+1.27, 2018 Δ+0.54, 2022 Δ+0.46) — su rol de protección es real —
pero HIERE en años normales/recuperación (2009 Δ−1.10, 2016 Δ−1.43, 2019 Δ−1.38,
2021 Δ−1.22, 2023 Δ−0.93): ir a flat en "risk-off" se pierde recuperaciones y
hace whipsaw.

**Conclusión honesta corregida.** El resultado de §18-21 (Sharpe 1.07 OOS con
régimen) era un **artefacto de la ventana corta de 5 años** (2021-2026), cuyos
regímenes específicos favorecían al filtro. El test largo y riguroso —el que el
desbloqueo de datos habilitó— lo refuta: **el filtro de régimen NO es un edge
robusto**; es una protección de crash que neta NEGATIVO a largo plazo. Ni la base
(Sharpe 0.79, PF 1.15) ni el régimen (0.61) pasan la batería §4 sobre 21 años.
Esto es el principio rector del goal en acción: la validación más honesta destruyó
el número bonito. Lección: **5 años no bastan** para validar un overlay de
régimen que actúa sobre eventos de cola raros.

## 24. Multi-clase (§2) — forex majors 21 años (Yahoo)

Con datos forex de Yahoo (`history_fx_yahoo.csv`, 38 772 filas, 7 majors
EUR/GBP/AUD/USD·JPY/CHF/CAD/NZD, 2005-2026, sha256) se corrió la misma estrategia
cross-sectional momentum-vol-target:

| Clase | Sharpe full | OOS (40%) | PF | maxDD |
| --- | --- | --- | --- | --- |
| ETF (21y) | +0.788 | +0.872 | 1.146 | 1.3% |
| **Forex majors (21y)** | **−0.147** | −0.110 | 0.974 | 2.4% |

**Lectura honesta.** La momentum cross-sectional **NO tiene edge en forex**
(Sharpe −0.15, PF <1) — el momentum de divisas se comporta distinto (más
mean-reverting / carry-driven) que el de ETFs. Además swaps/financing overnight
NO están modelados (los reales empeorarían el resultado). Limitación técnica
detectada: el filtro de régimen usa benchmark `SPY`, que no está en el universo
forex → es no-op ahí (por eso base==regime); para forex necesitaría un benchmark
de régimen propio (p. ej. DXY). Conclusión multi-clase: de las clases probadas
(ETF, forex), solo ETF muestra edge modesto —y sub-gate—; forex no. Futuros
pendiente (datos continuos de Yahoo poco fiables).

## 25. Línea base ETF sobre 21 años — scorecard §4 completo (el mejor resultado honesto)

Estrategia `momentum-vol-target` base (sin régimen) sobre 21 años (sanitizada OHLC):

| Gate | Valor | Umbral | Resultado |
| --- | --- | --- | --- |
| Sharpe | 0.788 | ≥1.0 | **FAIL** |
| Profit Factor | 1.146 | ≥1.3 | **FAIL** |
| Sortino | 1.036 | — | (>1.0) |
| Max drawdown | 1.3% | ≤15% | PASS |
| Monte Carlo DD p95 | 2.35% | ≤15% | PASS |
| DSR (n=1) | 0.9998 | >0 | PASS |
| trades | 11 598 | ≥100 | PASS |
| CAGR | 0.47% | — | (sizing 2%) |

**Lectura honesta — "lo que realmente tenemos".** La estrategia de reglas base es
robusta sobre 21 años (2005-2026, incluye todos los crashes): expectativa positiva,
drawdown mínimo (1.3%), DSR≈1, y **Sortino 1.04 >1.0** (la asimetría a la baja está
bien controlada; el Sharpe lo arrastra la volatilidad al alza). PERO **falla Sharpe
(0.79) y Profit Factor (1.15)** frente a los gates §4 → no promocionable bajo la
batería completa. Con el presupuesto de DD del 25% del operador podría escalarse
~18× el sizing (→ ~23% DD, ~8.5% CAGR) pero Sharpe/PF son invariantes a la escala.
Base honesta y defendible, no un pase de gate.

## 26. Multi-clase (§2) — cripto 2018-2026 (Yahoo): edge crudo destruido por costos

Datos cripto de Yahoo (`history_crypto_yahoo.csv`, 18 666 filas, 6 majors
BTC/ETH/LTC/XRP/BCH/DOGE, 2018-2026, 24/7, sha256). Momentum-vol-target
cross-sectional a distintos niveles de costo (cripto tiene costos altos):

| Costo (ida+vuelta) | Sharpe | OOS | PF | maxDD |
| --- | --- | --- | --- | --- |
| 1+1 bp (irreal para cripto) | 0.916 | 0.579 | 1.193 | 4.6% |
| 10+10 bp | 0.651 | 0.232 | 1.133 | 5.6% |
| **25+25 bp (retail realista)** | 0.209 | **−0.345** | 1.041 | 9.2% |

**Lectura honesta.** Cripto tiene **señal cruda de momentum** (Sharpe 0.92 a costo
casi cero) — más que ETF sin costos. PERO la estrategia tiene turnover alto (6394
trades) y **el edge se destruye con costos realistas de cripto**: a 25+25bps el OOS
Sharpe es NEGATIVO (−0.35). Regla del goal (backtest realista con costos): a costos
honestos, cripto tampoco pasa. Conclusión multi-clase completa: de las 3 clases
(ETF, forex, cripto), NINGUNA da edge robusto-a-costos que pase los gates §4 con
esta estrategia. Insight: el cuello de botella en cripto es el **turnover**.

**Validación del insight de turnover (cripto @ 25+25bps realista).** Variando
`momentum_window` (formación más larga = menos rotación):

| mw | Sharpe | OOS | PF | turnover |
| --- | --- | --- | --- | --- |
| 20 | 0.209 | −0.345 | 1.041 | 37.9 |
| 60 | 0.601 | +0.593 | 1.131 | 17.5 |
| 120 | 0.639 | **+0.702** | 1.153 | 12.9 |

Bajar el turnover **recupera la robustez a costos**: a formación larga (mw=120) el
OOS vuelve a +0.70. mw=60/90/120 todos positivos → región robusta, efecto económico
real (menos rotación = menos costo), no un pico de data-mining. AUN ASÍ sigue
**sub-gate** (0.70 <1.0, PF 1.15 <1.3). Conclusión honesta: momentum de baja
rotación en cripto es un edge real, cost-robusto y positivo, pero como en ETF
(0.79) no alcanza el umbral 1.0. Momentum en activos líquidos parece topar en
~0.7-0.8 Sharpe. Dirección futura legítima: reducir turnover también en ETF.

**Combinación ETF+cripto (probada, negativa).** El intento naive de fusionar
ETF+cripto en un universo (16 activos, mw=120, 25bps) da Sharpe −0.9 a −1.0 y
maxDD 24-36% — PEOR que cualquier clase sola. Causa honesta: el momentum
cross-sectional **concentra** en los activos más trendy (cripto) en vez de
diversificar → hereda la volatilidad/drawdown de cripto a costo alto. La
diversificación real requeriría **sleeves separados por clase con asignación fija
(risk-parity)**, no top-N momentum sobre un universo mezclado — cambio de
construcción de portafolio, no un ajuste de parámetro (dirección futura).

## 27. Sleeves risk-parity ETF+cripto (probado — INCONCLUSO, no éxito)

Test principiado: retornos de la sleeve ETF (bajo costo) y cripto (low-turnover,
25bps) por separado, vol-normalizados causalmente (igual contribución de riesgo)
y combinados. Resultado (2018-2026, ~3110 días):

| Métrica | Valor |
| --- | --- |
| Correlación ETF vs cripto | **+0.11** (baja → diversificación real posible) |
| Sharpe full-sample | 0.564 |
| Sharpe OOS (últ. 40%) | 1.362 |
| Profit Factor | 1.120 |
| **Max drawdown** | **62.9%** ⛔ |

**Lectura honesta — NO es un pase de gate, es inconcluso.** El único dato sólido
y alentador es la **baja correlación (0.11)**: la diversificación entre clases es
un free-lunch real y potencial. PERO: (1) el maxDD 62.9% es **inaceptable**
(el operador fijó 25%); (2) el Sharpe full-sample (0.56) contradice el OOS (1.36)
→ inestable; reportar el OOS 1.36 como "pasa el gate" sería cherry-picking, justo
lo que §0 prohíbe. La vol-normalización naive amplifica las colas. Conclusión: la
diversificación multi-clase es la dirección correcta (corr 0.11 lo confirma) pero
requiere una implementación con **control de riesgo real** (cap de leverage/vol,
límite de DD) que aún NO existe. No hay edge robusto y gate-passing todavía —
tampoco aquí. Es una dirección viva y honesta, no un resultado.

## 28. HALLAZGO — risk-parity ETF+cripto con vol-target (cap leverage 1.0): pasa Sharpe robustamente

El 62.9% DD de §27 venía ENTERO del leverage sin cap (escalar ARRIBA en baja vol).
Con **cap de leverage 1.0** (solo de-riesgo en alta vol, nunca apalanca) sobre la
misma combinación risk-parity:

| Gate §4 | Valor | Umbral | Resultado |
| --- | --- | --- | --- |
| Sharpe full-sample | **1.088** | ≥1.0 | **PASS** |
| Sharpe OOS (últ. 40%) | **1.380** | ≥1.0 | **PASS** |
| Max drawdown | 3.8% | ≤15% (op. 25%) | PASS |
| Monte Carlo DD p95 | 6.6% | ≤15% | PASS |
| DSR (6 trials) | 0.995 | >0 | PASS |
| Profit Factor | 1.205 | ≥1.3 | **FAIL** |
| trades | miles | ≥100 | PASS |

**Validación de robustez (a diferencia del filtro de régimen §18-23):**
- **Full-sample TAMBIÉN >1.0** (1.088), no solo OOS — el régimen fallaba aquí (0.61).
- **7/9 años positivos**; negativos solo 2018 (−1.69) y 2022 (−0.40), crashes de
  cripto — no inmune, pero neta fuerte positivo y se recupera.
- **Walk-forward 4 folds todos positivos**: [0.42, 1.46, 1.22, 1.06].
- **DSR 0.995** sobrevive deflación de 6 trials; robusto a cap 1.0/1.5/2.0
  (Sharpe 1.07-1.09).
- Corr de sleeves 0.11 (§27): la diversificación es el motor real; el vol-target
  (de-riesgo, causal, sin lookahead) controla el riesgo.

**Lectura honesta.** Es el **primer edge del proyecto que pasa el gate de Sharpe
de forma robusta** (full + OOS + walk-forward + DSR + MC), con drawdown excelente.
Falla SOLO el Profit Factor (1.21 <1.3) y tiene 2 años negativos (crashes cripto).
NO es un pase limpio de la batería completa, pero es cualitativamente distinto a
todo lo anterior (que topaba en 0.7-0.8 full-sample). Caveats: (1) usa cripto (no
en la lista explícita ETF/Forex/Futuros del goal, pero el operador preguntó por
ella); (2) costo ETF 1bp es razonable-optimista para ETFs líquidos, cripto 25bp
realista; (3) requiere implementar el portafolio multi-sleeve con vol-target en el
motor (hoy es script de sesión) + su propio walk-forward gobernado antes de
cualquier promoción. Artefacto: script de sesión sobre history_{long_yahoo_clean,
crypto_yahoo}.csv (sha256).

**Techo del Profit Factor (probado, no forzado).** Se intentó pasar el PF≥1.3 con
una variación PRINCIPIADA (formación larga consistente en ambas sleeves = menor
turnover), no búsqueda ciega. Resultado: empeora (Sharpe baja a 0.82-0.88) y el
**PF se queda en 1.14-1.15** — NO mejora. El PF de este enfoque momentum-
diversificado tiene un **techo robusto ~1.2**; no cruza 1.3 con cambios
principiados, y forzarlo con búsqueda de parámetros sería el data-mining que §0
prohíbe. Conclusión honesta y final del edge: la estrategia risk-parity ETF+cripto
pasa **5 de 6 gates §4 robustamente** (Sharpe full+OOS, MaxDD, MC, DSR, trades) y
el **PF (~1.2) es el único faltante**, con techo real ~1.2 < 1.3. Nota: el goal
marca "PF ≥ ~1.3" con "~" y "umbrales aprox., confirmar" — un PF 1.2 con Sharpe
1.09 y DSR 0.995 es una estrategia genuinamente buena; si el 1.3 es guía y no
gate duro, esto lo cumpliría; si es duro, queda a ~0.1 y es el límite honesto del
enfoque. Decisión de umbral: del operador.

**Stress mar-2020 / 2008 (§4, ahora con datos Yahoo desde 2005).** Antes marcado
data-blocked (feed IEX solo ~2020-07); con Yahoo se cierra el gap:

| Ventana | Sharpe | MaxDD | Trades |
| --- | --- | --- | --- |
| mar-2020 COVID crash (feb-abr) | +0.15 | **0.30%** | 102 |
| 2020 completo | +0.70 | 0.83% | 644 |
| 2008-09 GFC (sep-mar) | +0.16 | 0.50% | 315 |
| 2008 GFC full | −0.44 | 1.16% | 1447 |

Lectura: la estrategia ETF es flat/levemente positiva en los crashes agudos
(mar-2020, GFC agudo) y negativa en el GFC prolongado, pero el **control de
riesgo aguanta**: maxDD <1.2% incluso en 2008-2009. No revienta en estrés —
consistente con §16 (2022). Cierra el ítem mar-2020 del §4.

## 29. ¿Futuros (in-scope) reemplazan a cripto para diversificar? NO

Para responder honestamente si el edge se logra DENTRO del scope explícito del
goal (ETF/Forex/Futuros, sin cripto), se probó un sleeve de futuros (Yahoo
continuo front-month: ES/NQ/GC/CL/ZB/ZN/SI/6E, 43 285 filas, sha256; caveat: no
roll-ajustado) combinado con ETF:

| Métrica | ETF+Futuros | (ref ETF+Cripto §28) |
| --- | --- | --- |
| Correlación de sleeves | **+0.482** | +0.11 |
| Sharpe full | 0.749 | 1.088 |
| Sharpe OOS | 0.810 | 1.380 |
| Profit Factor | 1.137 | 1.205 |
| pos. years | 17/22 | 7/9 |

**Lectura honesta y decisiva.** Los futuros NO dan la diversificación que dio
cripto: la correlación ETF-futuros es **+0.48** (vs +0.11 de cripto) porque los
futuros de índices/bonos/commodities SOLAPAN con el universo ETF (ES≈SPY, ZB≈TLT,
GC≈GLD). El combinado ETF+futuros da Sharpe 0.75 — **sub-gate, y peor que ETF
solo** (0.79). Conclusión: **dentro del scope explícito del goal (ETF/Forex/
Futuros) NO hay edge que pase el gate**; el único edge gate-passing hallado
(Sharpe 1.09) **depende de la unicidad de cripto** (corr 0.11, clase genuinamente
descorrelacionada que activos tradicionales no replican). Esto hace la decisión
de aceptar cripto **decisiva, no opcional**: es la diferencia entre 0.75 (sub-
gate, in-scope) y 1.09 (gate-passing, requiere cripto).

## 30. Cripto aceptada por el operador → sensibilidad a costos REALES de Alpaca (2026-07-09)

El operador decidió: **sí a cripto con Alpaca** (goal actualizado 2026-07-09).
Pre-flight de ejecución real en cuenta paper (no solo datos): orden demo $10
BTC/USD → FILLED @63204.051 → cierre a plano → FILLED @63131.48. Hallazgos
operativos verificados: mínimo **$10 notional/orden** (error 40310000 por
debajo), TIF **GTC** (DAY no aplica a cripto), 24/7, los 6 símbolos del sleeve
§28 (BTC/ETH/LTC/BCH/DOGE/XRP en /USD) tradables y fraccionables; round-trip
observado ~11 bps de spread + fee taker 25 bps (tier base).

Con eso, sensibilidad del edge §28 al costo cripto real (CLI `sleeve-backtest`,
ventana 2018+, ETF fijo 1 bp; artefactos `reports/tmp/crypto/sleeve_2018_cost_*.json`):

| Costo cripto | Sharpe full | Sharpe OOS | PF | MaxDD |
| --- | --- | --- | --- | --- |
| 25 bps (§28 baseline) | 1.088 | 1.380 | 1.205 | 3.8% |
| **35 bps (fee+½spread, realista Alpaca)** | **1.017** | **1.282** | 1.190 | 3.9% |
| 45 bps (pesimista) | 0.947 | 1.183 | 1.176 | 4.0% |

**Lectura honesta.** El edge sobrevive el costo realista de Alpaca (35 bps):
Sharpe full 1.02 y OOS 1.28, ambos ≥1.0 — pero el margen full-sample queda FINO
(0.017 sobre el gate). En el escenario pesimista (45 bps) el full-sample cae
sub-gate (0.95) aunque el OOS aguanta (1.18). Implicaciones: (1) el sleeve
cripto debe ejecutarse low-turnover como está diseñado (formación 120d); (2)
conviene medir el costo efectivo real por trade durante Gate 1 en paper y
recalibrar; (3) ningún resultado aquí garantiza rentabilidad (§0) — es la mejor
estimación honesta con costos verificados en la cuenta real.

## 31. Adaptación de parámetros walk-forward: probada y RECHAZADA (2026-07-09)

Pedido del operador: "aumentar robustez y capacidad de adaptación". Se probó la
única adaptación honesta pendiente (§7b.1 del informe): re-selección del
momentum_window por ventana anual usando SOLO datos pasados (expanding, mínimo
1 año; candidatos {12,16,20,30,60,90,120}; retornos precomputados por candidato
— causal por construcción). Comparación contra los parámetros fijos de §28:

| Serie (2018/19+) | Sharpe | Sharpe OOS | PF | MaxDD |
| --- | --- | --- | --- | --- |
| Portfolio FIJO (§28: ETF 20 / cripto 120) | **1.088** | **1.380** | 1.205 | 3.8% |
| Portfolio ADAPTATIVO | 0.914 | 0.496 | 1.155 | 4.3% |
| Sleeve ETF fijo 20 / adaptativo | 0.876 / 0.991 | — | 1.165 / 1.189 | ~4.4% |
| Sleeve cripto fijo 120 / adaptativo | 0.639 / **0.307** | — | 1.153 / 1.058 | 4.4→6.7% |

**Lectura honesta.** La selección por mejor Sharpe pasado NO predice la mejor
ventana futura: en cripto elige persistentemente ventanas cortas (12-30) que el
régimen de costos castiga (§26), y a nivel portfolio pierde por mucho — incluso
con handicap a favor (la serie fija incluye 2018, año −1.69 de cripto, y aun así
gana). La leve mejora del sleeve ETF (0.99 vs 0.88, vía mw=12) no compensa y es
consistente con §13 (pico local en ventanas cortas) — adoptarla sería fiarse de
una señal que en cripto acaba de demostrar ser dañina. CONCLUSIÓN: la capacidad
de adaptación del sistema debe seguir en el nivel de RIESGO (vol-targeting
causal por sleeve + escala risk-parity entre sleeves, ya implementados y
operando), NO en el nivel de parámetros. Los parámetros fijos low-turnover de
§28 se mantienen. Artefacto: script de sesión sobre history_{long_yahoo_clean,
crypto_yahoo}.csv; reproducible con los mismos candidatos y folds anuales.

## 32. Sensibilidad al target de vol: plana bajo cap 1.0 (2026-07-09)

`sleeve-backtest` 2018+ con target_daily_vol ∈ {0.5%, 1%, 1.5%, 2%}: Sharpe
1.084/1.088/1.088/1.088 (OOS 1.380 en todos), PF 1.204-1.205, maxDD 3.8%.
El cap de leverage 1.0 domina la normalización (nunca apalanca), así que el
único parámetro de la capa de riesgo-paridad es efectivamente el cap — ya
probado robusto en 1.0/1.5/2.0 (§28). No hay parámetro frágil oculto en la
capa de combinación. Artefactos: reports/tmp/crypto/sleeve_tv_*.json.
