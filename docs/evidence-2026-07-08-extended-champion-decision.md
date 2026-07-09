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
