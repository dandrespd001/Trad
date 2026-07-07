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

| Candidato (lado extendido) | Status | Score neto de costos |
|---|---|---|
| champion_latest_model | OK | 0.0560 |
| logreg_default_plus_extended (con rsi_14/macd_hist/bb_pct_b) | OK | 0.0 |
| logreg_current_features | OK | -2.706 |
| sklearn_random_forest | OK | 0.0 |
| lightgbm / xgboost | SKIPPED (deps opcionales ausentes) | - |

**Recomendacion del reporte: `baseline`** (margen observado 0.0 < 5%
requerido). Los indicadores extendidos compitieron y no superaron al campeon
en esta ventana. La respuesta honesta del sistema de gobernanza es no
activarlos: garantizar rentabilidad empieza por no encender features que no
pagan su costo.

## Proximos pasos

- Repetir la medicion cuando el dataset aprobado se refresque (cadencia
  natural: semanal con la campania N0) y cuando lightgbm/xgboost esten
  instalados (candidatos hoy SKIPPED podrian explotar mejor las features
  nuevas que una logistica lineal).
- C3 (ponderacion del arbitraje por calidad medida de propuestas) queda a la
  espera de pares propuesta-outcome de la campania diaria.
