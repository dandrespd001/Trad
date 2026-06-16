# Model Evaluation Policy

Fecha: 2026-06-16

Esta politica define los gates minimos para que una estrategia o modelo avance
de research a paper trading. No autoriza live trading.

## Principios

- El baseline determinista es obligatorio.
- Ningun modelo reemplaza al champion sin evidencia out-of-sample.
- Los costos, comisiones, slippage y turnover se incluyen antes de comparar.
- Las features deben representar informacion disponible en el momento de la
  decision.
- LLM, RAG y sentiment solo generan features o explicaciones; no envian ordenes.

## Split temporal

1. Definir un rango de entrenamiento cerrado.
2. Definir un rango de validacion para seleccion de parametros.
3. Definir un rango out-of-sample que no se toca durante tuning.
4. Usar walk-forward cuando haya multiples regimenes.
5. Aplicar purging/embargo si las etiquetas se solapan.

## Metricas minimas

| Categoria | Metrica |
| --- | --- |
| Retorno | CAGR, retorno acumulado |
| Riesgo | Max drawdown, volatilidad anualizada, Sortino |
| Ajustado por riesgo | Sharpe, Calmar |
| Ejecucion | Turnover, costos, slippage estimado |
| Robustez | Numero de trades, exposicion, sensibilidad por regimen |
| Operacion | Errores, latencia, rechazos, drift |

## Gates antes de paper trading

- Backtest reproducible con version de datos y semilla fija.
- Baseline y challenger comparados out-of-sample.
- Resultado no depende de pocos trades aislados.
- Drawdown maximo y perdida diaria maxima definidos antes del test.
- Risk gate bloquea live trading por defecto.
- Stress test cubre datos faltantes, gaps y rechazo de orden simulado.

## Gates antes de live trading

Live trading queda explicitamente fuera de alcance. Para reconsiderarlo se
requieren, como minimo:

- 60-90 dias de paper trading estable en acciones/ETFs, o 90+ dias en futuros.
- Diferencia paper vs backtest explicada.
- Kill-switch probado.
- Reconciliacion broker probada.
- Revision manual de credenciales, permisos, impuestos y regulacion.
- Capital inicial pequeno y escalado por hitos documentados.
