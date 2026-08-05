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

---

# Addendum: H2 cripto y la combinación de sleeves

Datos obtenidos el 2026-08-05 del endpoint público de cripto de Alpaca, **sin
credenciales** (`configs/data_sources.yml: alpaca_crypto_data`, read-only).
2.038 sesiones, 2021-01-01 → 2026-07-31. XRP sólo tiene 943 sesiones porque
Alpaca lo listó tarde, así que se reporta con y sin él.

Corrección metodológica aplicada: cripto cotiza los 365 días del año.
`periods_per_year=365` en vez de 252; con 252 el Sharpe saldría inflado ~20%.

## H2 aislado

| Estrategia (6 pares, costes 25 bps) | Sharpe | CAGR | vol | MaxDD | exposición | turnover |
|---|---:|---:|---:|---:|---:|---:|
| Actual cross-sectional (top3, mom120, cap 2%) | 0,47 | 0,26% | 0,57% | 0,80% | **0,6%** | 2,3 |
| TSMOM N=1 | 0,28 | 2,57% | 11,27% | 19,72% | 16,4% | 52,8 |
| TSMOM N=63 | **0,43** | 4,30% | 11,40% | 23,18% | 16,2% | 10,7 |
| BTC buy & hold (contexto) | 0,52 | 14,56% | 57,69% | **76,68%** | — | — |

`PSR=0,843  DSR=0,775` con 8 trials. **NO PASA.**

La política desplegada opera al **0,6% de exposición** en cripto: un techo aún
más extremo que el 4,2% de ETF. TSMOM llega sólo al 16,4%, y eso es correcto —
con BTC al 57,69% de volatilidad anual, alcanzar un objetivo de cartera del 12%
no requiere mucho cripto.

Valor real aunque insuficiente: TSMOM recorta el drawdown de **76,68% a 23,18%**
frente a mantener BTC. Es gestión de riesgo genuina, pero el Sharpe sigue por
debajo del de simplemente mantener el activo.

## La combinación

Ventana común 2021-06-02 → 2026-07-07, 1.862 días naturales, blend risk-parity
reescalado a 12% de volatilidad de cartera:

| | Sharpe | CAGR | vol | MaxDD |
|---|---:|---:|---:|---:|
| ETF TSMOM N=63 | 0,67 | 7,08% | 11,12% | 14,13% |
| Cripto TSMOM N=63 | 0,44 | 4,65% | 11,91% | 23,18% |
| **Combinado risk-parity** | **0,70** | 7,93% | 12,00% | 16,36% |

`PSR=0,941  DSR=0,823` (14 trials). **NO PASA.** Falla incluso el PSR, que
ignora que hubo búsqueda.

**Corrección a la evidencia previa:** la correlación diaria ETF-cripto medida es
**0,280**, no el 0,11 que registraron los ciclos anteriores. Con esa correlación
y Sharpes de 0,67 y 0,44, el beneficio de diversificación es marginal: el
combinado (0,70) apenas supera al sleeve ETF solo (0,67). La tesis de que cripto
aporta un flujo de retorno sustancialmente ortogonal **no se sostiene** con datos
propios y motor causal.

## Veredicto consolidado

| Estrategia | Sharpe neto | DSR | ¿pasa? |
|---|---:|---:|---|
| Política desplegada, costes reales | **−0,30** | — | No |
| H1 ETF TSMOM | 0,67 | 0,861 | No |
| H2 cripto TSMOM | 0,44 | 0,775 | No |
| H1+H2 combinado | 0,70 | 0,823 | No |
| *SPY buy & hold* | *0,76* | — | *—* |

**Ninguna hipótesis supera el Gate 3, y ninguna supera a mantener SPY.** El único
camino no agotado es la amplitud: el universo de ~45 ETF multi-activo que la
evidencia externa sostiene, y que sigue bloqueado por credenciales.
