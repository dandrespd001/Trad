# Cribado de desarrollo 2026-08-05: TSMOM frente a la política actual

**NO PROMOVIBLE.** La fuente es `data/incoming/history_5y.csv`: barras diarias
Alpaca IEX auténticas (`provider: alpaca_market_data`, `feed: iex`, `status: OK`,
sin blockers) pero con **sidecar schema 1.0**, y el contrato congelado
`iex-1.0` exige 1.1 con `source_sha256` y `published`. El contrato admite datos
menores exclusivamente para desarrollo retrospectivo no promovible; esto es eso.
Ningún número de este documento puede aprobar una promoción.

Ventana: 2021-06-01 → 2026-07-07, 1.280 sesiones, los 10 ETF de
`configs/universe.yml`.

## Resultado

| Estrategia | costes | Sharpe | CAGR | vol | MaxDD | exposición | turnover |
|---|---|---:|---:|---:|---:|---:|---:|
| Actual cross-sectional (top3, mom20, cap 2%) | 2 bps | 0,37 | 0,29% | 0,79% | 1,32% | 4,2% | 20,8 |
| Actual, **techo liberado** (cap 30%) | 2 bps | **0,37** | 3,77% | 11,84% | 19,04% | 63,2% | 311,0 |
| Actual cross-sectional (cap 2%) | **15 bps** | **−0,30** | −0,24% | 0,79% | 2,36% | 4,2% | 20,8 |
| Actual, techo liberado | **15 bps** | **−0,30** | −4,18% | 11,86% | 32,12% | 63,2% | 311,0 |
| H1 TSMOM diario | 15 bps | 0,25 | 2,07% | 10,24% | 16,15% | ~70% | 94,8 |
| H1 TSMOM, rebalanceo N=63 | 15 bps | 0,67 | 7,12% | 11,14% | 14,06% | ~70% | 13,7 |
| SPY buy & hold (contexto, nunca gate) | — | 0,76 | 12,06% | 16,85% | 25,38% | 100% | — |

## Lo que queda demostrado

**1. El techo de exposición era un problema de escala, no de señal.** Liberar
`max_single_position` movió la exposición de 4,2% a 63,2% y la volatilidad de
0,79% a 11,84% —por fin alcanzando el objetivo del 12%— y multiplicó el CAGR por
trece. **El Sharpe no se movió: 0,37 en ambos casos.** Esto confirma
mecánicamente lo que la campaña de sizing observó sin explicar. Corregir el techo
es condición necesaria para que un edge se convierta en dinero, y es
estrictamente insuficiente para crear edge.

**2. Los costes reales invierten el signo de la estrategia actual.** A los 15 bps
medidos, la política desplegada rinde Sharpe **−0,30**, no un Sharpe pequeño y
positivo. Con el techo liberado el turnover sube a 311 anual, que a 13 bps de
coste no modelado son ~400 bps/año: cualquier señal de esta familia queda
sepultada. El coste no es un ajuste marginal sobre el resultado; es el resultado.

**3. El control de turnover es mecanismo real; el nivel de Sharpe es ruido.** El
*hueco* entre costes asumidos y medidos decae monótonamente al espaciar el
rebalanceo (0,24 → 0,10 → 0,07 → 0,05 → 0,04 → 0,03). Eso es estructura. Pero el
Sharpe absoluto salta 0,25 / 0,51 / 0,47 / 0,52 / 0,35 / 0,67 sin patrón: la
no-monotonicidad en N=42 delata muestreo, no señal. **El N no puede elegirse de
este cribado**; debe pre-registrarse y validarse fuera de muestra.

**4. Ni el mejor caso pasa el gate.** Aplicando el deflactor del propio
repositorio sobre la rejilla de 6 configuraciones:

```
Mejor trial: N=63, Sharpe anualizado 0,70
observaciones=1279  skew=−1,12  kurtosis=32,94
PSR (ignora que hubo búsqueda) = 0,936
DSR con n_trials=6             = 0,861   gate 0,95 -> NO PASA
DSR con n_trials=12            = 0,831   NO PASA
DSR con n_trials=24            = 0,803   NO PASA
```

Falla incluso antes de penalizar la búsqueda: el PSR ya está por debajo de 0,95.
La kurtosis de 32,94 con skew −1,12 indica cola izquierda gruesa, coherente con
lo observado en ciclos anteriores.

## Veredicto

**Ninguna hipótesis supera el Gate 3 sobre 10 ETF y 5 años.** TSMOM es
direccionalmente superior a la política actual —Sharpe mayor, turnover 7 veces
menor, y sobrevive a los costes con signo positivo en vez de negativo— pero 0,67
frente a un umbral de 1,0 no es un candidato, y queda por debajo del 0,76 de
simplemente mantener SPY en la misma ventana.

## Qué falta antes de concluir sobre TSMOM

La tesis de TSMOM descansa en **amplitud**, y este cribado no la tiene. Diez ETF
—siete de renta variable estadounidense fuertemente correlacionados, más TLT y
GLD— no son una cartera multi-activo. La evidencia externa de Sharpe neto
0,76-0,95 corresponde a trend-following diversificado sobre decenas de mercados
en renta variable, bonos, materias primas y divisas.

Además, la ventana 2021-2026 contiene un mercado bajista y una tendencia alcista
fuerte, pero también el rango lateral de 2023 donde el trend-following
notoriamente sufre. Cinco años son un fold, no una validación.

Bloqueo concreto: ampliar el universo a ~45 ETF exige regenerar el dataset con
credenciales read-only de Alpaca Market Data, que no están provisionadas en el
entorno. Hasta entonces, la hipótesis principal no ha sido probada, sólo acotada.
