# P0-03 — Causalidad, costes, calendario y cash del backtest

## Decisión de seguridad

Toda evidencia económica producida con el contrato anterior queda invalidada
para promoción. El sistema permanece en investigación/paper,
`promotion_eligible=false` y `live_trading_allowed=false`. Este paquete corrige
el simulador; no demuestra rentabilidad ni autoriza órdenes reales.

## Defectos confirmados

1. Una señal calculada con el cierre de `t` recibía el retorno completo
   `close(t) → close(t+1)`, incluido un gap ocurrido antes de que la orden
   pudiera ejecutarse.
2. El único argumento all-in `cost_bps` se copiaba en `cost_bps` y
   `slippage_bps`, cobrando dos veces el escenario indicado.
3. Al agregar ETF y cripto, una sleeve sin sesión desaparecía del denominador;
   su presupuesto se reasignaba implícitamente a la sleeve abierta.
4. `start_date` eliminaba la historia previa antes de normalizar volatilidad,
   reiniciando de hecho el warmup en la fecha solicitada.
5. El CLI llamaba `profit_factor` a un cociente de ganancias/pérdidas por
   periodo, aunque no existía atribución PnL por trade.
6. La supuesta DSR fijaba `n_trials=1` y varianza cero pese a que se habían
   inspeccionado múltiples parámetros y variantes.

## Contrato corregido

### Tiempo de señal y ejecución

- La señal se observa en el cierre de la sesión `t`.
- La primera ejecución simulada ocurre en la apertura de la siguiente barra
  disponible de esa sleeve, no en el cierre ya observado ni en la siguiente
  fecha civil.
- La cartera antigua absorbe `close(t) → open(t+1)`.
- El turnover se calcula contra los pesos que han derivado con ese gap.
- La cartera objetivo sólo absorbe `open(t+1) → close(t+1)`.
- Un `open` ausente, cero, negativo o no finito bloquea el cálculo; nunca se
  sustituye por `close`.
- El artefacto declara `engine_version=next_open_v2` y la convención temporal.

### Costes

- En los comandos legacy de sleeves, el valor de entrada significa
  `total_one_way_cost_bps` all-in.
- Ese valor se cobra exactamente una vez sobre turnover de ejecución y se
  mapea a `BacktestConfig.cost_bps`; `slippage_bps=0` en ese borde legacy.
- El motor sigue permitiendo fee/coste y slippage separados para llamadas que
  realmente los configuren de forma independiente, y reporta ambos más el
  total.
- Un caso determinista con turnover `1.0` y coste total `25 bps` debe producir
  un débito de `0.0025` antes de retornos posteriores.

### Calendario y cash

- Cada sleeve conserva únicamente las barras de su calendario nativo.
- La unión de fechas ocurre al agregar sleeves.
- Si una sleeve está cerrada, aporta retorno cero; su presupuesto fijo queda en
  cash y no aumenta automáticamente el riesgo de otra sleeve.
- `start_date` filtra únicamente la salida, después de calcular el warmup con
  la historia anterior disponible.

### Métricas y promoción

- El schema de `sleeve-backtest` pasa a `2.0`.
- `return_gain_loss_ratio` sustituye la etiqueta incorrecta
  `profit_factor`; por compatibilidad de shape, `profit_factor=null` con
  `profit_factor_status=UNAVAILABLE_NOT_TRADE_LEVEL`, nunca un número engañoso.
- `deflated_sharpe=null` y
  `deflated_sharpe_status=UNAVAILABLE_NO_TRIAL_REGISTRY` hasta disponer de un
  ledger append-only con todos los experimentos, incluidos los descartados.
- El artefacto declara `research_only=true`, `promotion_eligible=false` y el
  blockers estables `deflated_sharpe_trial_registry_missing` y
  `trade_level_profit_factor_unavailable`.
- Un exit code cero sólo significa que el reporte se generó correctamente; no
  significa que la estrategia haya pasado un gate de promoción.

## Pruebas de aceptación

- Entrada nueva después de un gap: no captura el gap.
- Posición antigua: sí captura el gap antes del rebalanceo.
- Rotación A→B: turnover contra pesos pretrade derivados.
- Pesos de cierre: la deriva intradía se conserva para el siguiente turnover,
  sin alterar la salida de targets usada por el planificador.
- Apertura faltante o inválida: error estable y fail-closed.
- Coste all-in: una sola carga conocida.
- Fin de semana/festivo: avanza entre barras reales de la sleeve.
- Sleeve cerrada: presupuesto fijo en cash.
- `start_date`: usa historia anterior para normalización causal.
- Artefacto CLI: schema, timing, coste, nombres de métricas y blocker de DSR.
- Revalidación continua: usa el mismo contrato sin duplicar costes ni afirmar
  elegibilidad económica.

## Verificación local

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv312/bin/python -m unittest \
  tests.test_data_features_backtest \
  tests.test_signal_policy_backtest \
  tests.test_portfolio_sleeves \
  tests.test_sleeve_allocate \
  tests.test_sleeve_revalidation -v

.venv312/bin/python -m ruff check \
  src/trading_ai/backtest/engine.py \
  src/trading_ai/backtest/portfolio.py \
  src/trading_ai/cli.py \
  src/trading_ai/execution/sleeve_revalidation.py \
  tests/test_data_features_backtest.py \
  tests/test_signal_policy_backtest.py \
  tests/test_portfolio_sleeves.py \
  tests/test_sleeve_revalidation.py --select E9,F63,F7,F82

git diff --check
```

## Resultado de verificación — 2026-07-14

- Batería dirigida: **73 tests OK** en `0.731s`.
- Regresión completa: **1445 tests OK** en `122.242s`.
- `py_compile`: OK para todos los módulos y tests modificados.
- Ruff crítico `E9,F63,F7,F82`: OK en todos los archivos modificados.
- Ruff adicional `F401,B905`: OK en los cuatro módulos de producción.
- `git diff --check`: OK.
- `configs/risk.yml`: `live_trading_allowed: false` confirmado.

La suite completa emite mensajes de bloqueo y warnings que forman parte de sus
casos negativos; el proceso terminó con exit code cero. No se llamó al broker,
no se enviaron órdenes y no se modificó configuración live.

Este resultado cierra la implementación técnica P0-03, pero no restaura la
evidencia económica. Los artefactos v1 de `reports/tmp/backtest/sleeve_*`, las
sensibilidades de coste/vol-target, allocations antiguas y revalidaciones que
dependían del envelope `0.066` continúan
`INVALIDATED_FOR_PROMOTION`. Deben regenerarse sobre snapshots aprobados con un
protocolo prerregistrado y un ledger completo de trials.

## Evidencia externa adoptada

- Backtrader documenta que una market generada desde una barra ya cerrada se
  ejecuta en la apertura de la siguiente barra:
  <https://www.backtrader.com/docu/order-creation-execution/order-creation-execution/>.
- Alpaca expone un calendario de sesiones con aperturas, cierres y cierres
  anticipados: <https://docs.alpaca.markets/reference/querymarketcalendar>.
- Alpaca documenta trading cripto continuo, con reglas distintas del calendario
  de acciones: <https://docs.alpaca.markets/us/docs/crypto-trading>.
- Bailey y López de Prado describen la DSR y la corrección necesaria por
  múltiples pruebas: <https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf>.
- Alpaca advierte que paper no modela impacto, latencia, posición en cola,
  price improvement ni varias fricciones reales:
  <https://docs.alpaca.markets/us/docs/paper-trading>.

La política de mantener exactamente el presupuesto de la sleeve cerrada en
cash es una decisión conservadora local y debe permanecer prerregistrada; no se
atribuye a estas fuentes como requisito externo.
