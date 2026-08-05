# Decisión 2026-08-05: congelar P0-05 y pivotar el presupuesto a investigación de alfa

Estado: **ADOPTADA**. Sustituye la priorización implícita del plan de
reactivación del 2026-07-27, que ordenaba cerrar P0-05 antes de volver a
investigación económica.

## Contexto

El repositorio tiene una infraestructura de gobernanza y ejecución
desproporcionadamente madura —1.980 pruebas, 85% de cobertura, motor causal
`next_open_v2`, ledger encadenado, pre-registro anti-p-hacking, kill-switches
con estado real— y **ninguna evidencia de edge**. Las dos campañas
pre-registradas que llegaron a ejecutarse terminaron rechazadas:

- `p0-03-etf-technical-fd8b11a8dc0c`: retorno activo compuesto **−5,64%** frente
  a SPY, Sharpe activo −0,96, probabilidad bootstrap de activo positivo 30,7%.
  `REJECTED_RESEARCH_CANDIDATE`.
- `etf-sizing-campaign-v1`: `NO_CANDIDATE`, DSR del baseline **0,203** contra un
  umbral de 0,95; las seis variantes quedaron más de 13 puntos por debajo de SPY.

El desequilibrio arquitectónico es medible: 77 archivos en `execution/` contra 6
en `research/`.

## Diagnóstico: tres fallos apilados

La lectura previa —"falta alfa"— es incompleta. Sólo el tercer fallo es de señal.

### 1. Techo de despliegue de capital (configuración, no alfa)

En `src/trading_ai/backtest/engine.py`, `_target_weights()`:

```python
raw_weight = min(cfg.max_gross_exposure / len(selected), cfg.max_single_position)
```

Con `configs/risk.yml: max_single_position: 0.02` y `top_n: 3`, el segundo
término gana siempre: el techo duro de exposición bruta es **6%**. La media
medida en la campaña de sizing fue 4,83%.

La consecuencia es que el vol-targeting **nunca liga**. `BacktestConfig`
declara `target_annual_volatility = 0.12`, pero la vol realizada del baseline es
**0,66%** —dieciocho veces por debajo del objetivo— y el escalar de vol sólo
puede reducir, nunca aumentar. El CAGR queda acotado mecánicamente en torno al
0,4% **con independencia de la calidad de la señal**.

La campaña de sizing confirmó esto sin nombrarlo: T05 triplicó la exposición
(4,83% → 14,49%) y el Sharpe se mantuvo en 0,57. El sizing es un multiplicador
de la señal, no una fuente de edge. Esa conclusión ya está registrada; lo que
faltaba era la causa mecánica.

### 2. Modelo de costes subestimado 7,5x

El backtest asume 2 bps all-in (`cost_bps: 1.0` + `slippage_bps: 1.0`). Los
fills reales de paper midieron **15 bps de mediana**
(`docs/revision-operaciones-2026-07-14.md`). Con el turnover observado de 4,55
anual, la diferencia son ~59 bps/año no modelados, superior al CAGR completo del
baseline.

Toda validación económica ejecutada con 2 bps es inválida por una segunda razón,
independiente de los defectos del motor v1 que motivaron P0-03.

### 3. Fallo de alfa, real pero de diseño

Neutralizados 1 y 2, la calidad de la señal es Sharpe ≈0,57 y el gate exige
≥1,0. Momentum **cross-sectional** top-3 sobre diez ETFs mega-líquidos con
ventana de 20 días es simultáneamente el diseño más saturado y el más sensible a
costes disponible. La evidencia externa es consistente: los efectos documentados
en ETFs sectoriales sobreviven sólo por debajo de 2-3 bps de coste de
transacción.

### 4. Encuadre del benchmark

Medir una estrategia con 5% de exposición contra SPY al 100% en retorno activo
mide sobre todo *estar en efectivo*. El −5,64% y el −13,7% registrados no
distinguen entre mala selección y no estar invertido.

## Decisión

1. **Congelar P0-05.** La re-arquitectura del executor IPC queda en contención:
   timers `disabled`, executor en `reduce_only`, `live_trading_allowed: false`.
   No se cierran los diez ítems pendientes de
   `docs/remediation/p0-05-account-authority-partial.md` en este ciclo.
2. **Mover el presupuesto a investigación de alfa**, en este orden: agotar los
   datos gratuitos disponibles, reparar los instrumentos de medición, y sólo
   entonces probar hipótesis de señal nuevas y pre-registradas.
3. **Redefinir el producto** como retorno absoluto medido contra efectivo, no
   como competidor de SPY.

Justificación: un executor perfectamente seguro sobre una estrategia sin edge no
genera ingresos, y consume el presupuesto que necesita el único trabajo capaz de
cambiar el resultado económico. La congelación no relaja ninguna frontera de
seguridad: las mantiene exactamente donde están.

## Decisiones del operador (2026-08-05)

| Dimensión | Decisión | Consecuencia |
|---|---|---|
| Producto | Retorno absoluto vs efectivo | Gate: Sharpe neto ≥1,0, DD ≤12%, vol objetivo 10-12%. SPY pasa a referencia informativa, nunca criterio de rechazo |
| Datos | Sólo fuentes gratuitas | Alpaca IEX diario y cripto. Sin universo point-in-time ni delistados: el universo queda restringido a ETFs vivos, y el sesgo residual se declara |
| Capital real | <25.000 USD | **La regla PDT aplica y no está implementada**: cero coincidencias de `pattern_day`/`day_trade`/`PDT` en `src/`, `tests/` y `configs/` |
| Working tree | Commitear y publicar | Ejecutado: 122 commits publicados, historia segmentada por frontera |

## Consecuencias

**Habilitado.** El camino gratuito es viable y estaba infrautilizado:
`fetch-market-data --from/--to` no impone límite de historia, y
`data/incoming/history_5y.csv` prueba que ya se hicieron extracciones
multi-año. El snapshot de cuatro meses que bloqueaba las campañas por
insuficiencia de folds era autoinfligido, no un límite de la API.

**Bloqueado y aceptado.** Sin datos de pago no hay universo point-in-time, ni
delistados, ni futuros, ni forex, ni intradía real. Las configuraciones
`futures_micro.yml` y `forex_major.yml` siguen siendo placeholders sin feed.

**Nuevo trabajo obligatorio.** El guardarraíl PDT pasa a ser bloqueante para
cualquier promoción a real, no una mejora opcional. Las salidas intradía por ATR
de `paper_position_watch.py` pueden generar day-trades y deben consultarlo.

**Riesgo del feed IEX, declarado.** IEX representa una fracción pequeña del
volumen consolidado y su print de apertura es delgado. Como el motor ejecuta al
open siguiente, la desviación del OHLC de IEX respecto al consolidado incide
justo en el precio de ejecución. Requiere prueba de sensibilidad explícita.

## Qué revertiría esta decisión

- Que una hipótesis pre-registrada supere el Gate 3 (Sharpe neto ≥1,0 contra
  efectivo a costes medidos, DSR ≥0,95 con `n_trials` real, PBO ≤0,2, DD ≤12%,
  ≥252 observaciones OOS, bootstrap p05 positivo). En ese caso P0-05 vuelve a ser
  la ruta crítica, porque pasa a existir algo que vale la pena ejecutar.
- Una decisión del operador de financiar datos de pago, que reabriría multi-activo.

## Desenlace admitido

Dada la evidencia acumulada, existe probabilidad material de que ninguna
hipótesis supere el Gate 3. Este plan está construido para descubrirlo rápido y
barato. Si ocurre, la respuesta correcta es **no operar con dinero real** y
documentar el cierre. Ese desenlace es un resultado válido del método, no un
fallo de ejecución.
