# Auditoría de preparación multi-clase (§2) — 2026-07-08

Entregable parcial del §2 (config por clase de activo). Estado auditado contra
`configs/futures_micro.yml` y `configs/forex_major.yml`. Regla §8: no inventar
valores; los desconocidos/variables se marcan `TODO: verificar`.

## Resumen

El **scaffolding de config por clase existe** con los specs de instrumento
correctos; falta cerrar parámetros **placeholder** (margen, rollover, financing)
y —bloqueo duro— **no hay datos de mercado de futuros/forex integrados**, así que
ningún backtest de esas clases puede correr todavía. `live_trading_allowed:
false` en ambos (correcto). Plataformas marcadas research-only
(LEAN_IBKR / OANDA_PRACTICE), sin ejecución.

## Futuros (`futures_micro.yml`) — MES, MNQ

| Parámetro §2 | Estado |
| --- | --- |
| tick_size | ✅ MES 0.25 / MNQ 0.25 (estándar CME) |
| tick_value | ✅ MES $1.25 / MNQ $0.50 (multiplicadores $5 / $2 por punto) |
| comisión | ✅ 0.62/contrato (plausible retail; confirmar con broker real) |
| slippage | ✅ 1 tick (conservador, razonable) |
| calendario/horario | ◑ `equities_extended`, TZ NY — falta detalle de sesión CME (casi 24h, mantenimiento diario) `TODO: verificar` |
| **margen** | ⛔ `placeholder_usd` (1500/1800) — los fija CME y varían; `TODO: verificar` contra CME/broker actual |
| **rollover** | ⛔ `quarterly_volume_placeholder` — falta regla real (roll por volumen/OI el trimestre) `TODO: verificar` |

## Forex (`forex_major.yml`) — EURUSD, USDJPY

| Parámetro §2 | Estado |
| --- | --- |
| pip_size | ✅ EURUSD 0.0001 / USDJPY 0.01 (correcto) |
| lot_size | ✅ 1000 (micro-lote) |
| sesiones | ✅ 24x5 UTC (correcto para spot) |
| spread | ◑ 0.8/0.9 pips (plausible majors; `spread_watch: required` — bien) |
| slippage | ✅ 0.2 pips |
| **swaps/financing overnight** | ⛔ `financing_model: placeholder` — falta modelo real de swap (tasas por par/lado) `TODO: verificar` |
| gaps de fin de semana | ⛔ no modelado — el §4 lo exige para forex `TODO` |

## Bloqueos y próximos pasos (honestos)

1. **Datos de mercado**: NO hay feed integrado de futuros ni forex. Alpaca (el
   único proveedor autorizado) da equities/ETF, no futuros/forex. Sin datos, no
   hay backtest multi-clase. **Requiere que el operador provea/autorice una
   fuente** (p. ej. IBKR/LEAN para futuros, OANDA para forex — ya marcados como
   plataformas objetivo research-only).
2. **Cerrar placeholders** (margen futuros, rollover, financing forex) contra
   documentación oficial una vez haya proveedor — no inventar.
3. Cuando (1) y (2) estén, replicar sobre esas clases la MISMA batería de
   validación ya construida para ETF (walk-forward, DSR, Monte Carlo,
   sensibilidad, stress, scorecard §4) — la infraestructura es reutilizable.

**Conclusión honesta.** La dimensión multi-clase del goal está **preparada en
config pero no ejecutable**: specs de instrumento correctos, placeholders
honestamente marcados, y un bloqueo duro de datos que es decisión del operador.
No se reporta ninguna métrica de futuros/forex porque no existe ejecución real
que la respalde (regla §0).
