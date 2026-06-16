# Tooling Risk Register

Fecha: 2026-06-16

Este registro convierte la investigacion en controles operativos. La regla base
es simple: primero research local, despues paper trading, y live trading queda
fuera de alcance hasta que existan threat model, auditoria y aprobacion manual.

| Herramienta/capacidad | Fase permitida | Riesgo | Uso aprobado | Control requerido |
| --- | --- | --- | --- | --- |
| Python stdlib + modulos `src/trading_ai` | Fase 1 | Bajo | Metricas, risk gates y notebooks sin broker | Tests locales antes de cambios |
| pandas/polars/pyarrow | Fase 1 | Bajo/medio | Ingestion, limpieza y datasets Parquet | Hash de dataset y validacion de columnas |
| scikit-learn/statsmodels | Fase 1 | Medio | Baselines estadisticos y ML simple | Walk-forward y separacion train/test |
| LightGBM/XGBoost | Fase 1 | Medio | Modelos tabulares challenger | Limite de trials, costos incluidos, no leakage |
| MLflow | Fase 1 | Medio | Tracking y model registry local | Registrar dataset hash, parametros y metricas |
| Evidently/pandera | Fase 1-2 | Medio | Calidad, drift y validacion de datos | Reporte versionado por corrida |
| Optuna | Fase 1 | Alto si se usa libremente | Tuning limitado | Max trials, purging/embargo, OOS obligatorio |
| TimesFM/Chronos | Fase 1 | Medio/alto | Forecasting challenger | Comparar contra baseline naive y LightGBM |
| LLM local/FinGPT/RAG | Fase 1-2 | Alto | Resumen, sentiment, explicacion y auditoria | Sin autoridad de ordenes; timestamps estrictos |
| Alpaca paper/IBKR paper | Fase 2 | Alto | Paper trading y reconciliacion | Allowlist de simbolos, limites, kill-switch |
| MCP broker/trading | No permitido ahora | Critico | Ninguno en Fase 1 | Requiere paper-only y revision separada |
| Broker live | No permitido ahora | Critico | Ninguno | Requiere aprobacion manual fuera de este scaffold |

## Checklist antes de agregar dependencias

1. Revisar licencia, repositorio, maintainer y actividad.
2. Fijar version o lockfile antes de usar en experimentos.
3. Confirmar que no requiere secretos para pruebas locales.
4. Ejecutar primero con datos sinteticos o historicos no sensibles.
5. Registrar la decision en este archivo si cambia el nivel de riesgo.
