# Evidencia: Activacion de Indicadores Extendidos (medicion real)

**Fecha:** 2026-07-07
**Auditor:** Fable 5 (Arquitecto)
**Dataset:** `data/raw/approved/core_etfs/1d` (aprobado, 10 ETFs, 6200 filas
diarias 2024-01-02 a 2026-06-23)

## Pregunta

Los indicadores tecnicos extendidos (RSI-14, MACD 12/26/9, Bollinger %B)
estan implementados pero apagados por defecto. La pregunta del goal de
calidad de decision es: activarlos mejora el edge neto de costos?

## Hallazgo de revision previo a la medicion

La primera corrida del reporte dio scores identicos en ambos lados porque
ningun candidato del benchmark usaba las columnas extendidas
(`DEFAULT_MODEL_FEATURE_CANDIDATES` las excluye a proposito). La comparacion
era estructuralmente vacua; tests sinteticos en verde no lo revelaron, la
corrida con datos reales si. Corregido: el lado extendido presenta ademas el
candidato `logreg_default_plus_extended` (union de features default +
extendidas), evaluado por la misma maquinaria del benchmark.

## Resultado medido (comparacion ya sensible)

Segunda corrida el mismo dia con los extras `ml` instalados
(lightgbm 4.6.0, xgboost 3.3.0) para que ningun candidato quede SKIPPED —
medicion completa, todos con status OK:

| Candidato (lado extendido) | Status | Score neto de costos |
|---|---|---|
| champion_latest_model | OK | +0.0560 |
| logreg_default_plus_extended (con rsi_14/macd_hist/bb_pct_b) | OK | 0.0 |
| logreg_extended_technical | OK | 0.0 |
| sklearn_random_forest | OK | 0.0 |
| xgboost_classifier | OK | -2.181 |
| logreg_current_features | OK | -2.706 |
| lightgbm_classifier | OK | -3.093 |

(Lado baseline: mismos ordenes de magnitud; champion +0.0560 gana tambien.)

**Recomendacion del reporte: `baseline`** (margen observado 0.0 < 5%
requerido), ahora como respuesta DEFINITIVA para esta ventana: todas las
clases de candidato compitieron. Los indicadores extendidos no generan edge
neto de costos, y los modelos de arboles pierden dinero activamente en estos
datos (sobreajuste/churn con costos). La respuesta honesta del sistema de
gobernanza es no activarlos: garantizar rentabilidad empieza por no encender
features ni modelos que no pagan su costo — el campeon promovido sigue
siendo, con evidencia, la mejor decision disponible.

## Proximos pasos

- Repetir la medicion con cada refresco del dataset aprobado (cadencia
  natural: semanal con la campania N0); la recomendacion puede cambiar con
  regimenes de mercado distintos.
- C3 (ponderacion del arbitraje por calidad medida de propuestas) queda a la
  espera de pares propuesta-outcome de la campania diaria.

## Actualizacion 2026-07-08: con 5 anios de historia la recomendacion CAMBIA

Tras ampliar el dataset aprobado a 5+ anios (12,800 filas, 2021-06-01 a
2026-07-07, fetch gobernado Alpaca IEX), la misma medicion da:

| Lado | Mejor score neto de costos |
|---|---|
| baseline | 33.778 |
| extended (con rsi_14/macd_hist/bb_pct_b) | 45.384 |

**Recomendacion: `extended`** (margen relativo +34.4% >> 5% requerido).
Artefacto: reports/tmp/indicator_activation/2026-07-08/activation.json.

Lectura: con 2.5 anios los indicadores extendidos no separaban senal del
ruido; con 5 anios (que incluyen el ciclo 2022) el candidato que los usa
supera al campeon con margen amplio. La activacion por evidencia hizo su
trabajo en ambas direcciones. Camino a produccion: promocion gobernada de un
nuevo campeon entrenado con las features extendidas sobre los 5 anios
(model_run/model_eval/promotion con reviewer humano), NO un cambio directo
de configuracion.
