# Revisión de operaciones Gate 1 — señales, entradas, salidas y resultados (2026-07-14)

Auditoría pedida por el operador: qué abrió cada posición, con qué inputs, y
qué resultado dio. Fuentes: órdenes reales del broker, artefactos de ciclo
(`cycle_*_<fecha>.json`), datasets gobernados, y recálculo independiente de
las señales.

## 1. Qué "modelo" abre las posiciones (respuesta directa)

**No es un LLM ni un modelo ML.** Los modelos ML (logístico, LightGBM, con
todas sus variantes de features/etiquetas) fueron evaluados exhaustivamente y
NINGUNO superó al naive (evidencia §§1-15) — por eso NO operan. Lo que opera es
un **algoritmo determinista de reglas** validado con la batería completa (§28):

1. **Señal (momentum cross-sectional):** cada día, para cada símbolo del
   universo, retorno de los últimos 20 días (ETF) o 120 días (cripto). Se
   seleccionan los **3 mejores con momentum positivo**. Sin momentum positivo
   → cash (por eso cripto lleva flat desde el día 1: BTC −9.9%…BCH −47.7%).
2. **Tamaño (vol-targeting):** peso base 0.10 por símbolo, escalado por
   `min(1, 12%/vol_realizada_20d)` — si la volatilidad del grupo sube, el
   tamaño BAJA automáticamente (nunca sube de 0.10).
3. **Capital (risk-parity, M4):** presupuesto por sleeve = (total/2) ×
   escalar de vol trailing (cap 1.0), sobre el total efectivo que fija la
   re-validación (M14).
4. **Ejecución:** delta contra la posición real → orden (limit-maker en
   cripto, market en ETF), pasando por kill-switches con estado real (M7),
   cap riesgo-a-stop (M12), mínimos y price-sanity.

## 2. Señales verificadas contra sus inputs (recálculo independiente)

| Decisión | Top-5 momentum 20d (inputs reales) | Selección | Vol grupo | Escalar | Peso |
| --- | --- | --- | --- | --- | --- |
| 07-09 | XLF +5.8%, XLV +4.9%, IWM +4.3%, XLI +3.1%, XLK +2.5% | XLF/XLV/IWM | 10.9% | 1.00 | 0.100 |
| 07-10 | XLI +7.3%, XLF +6.7%, XLK +5.3%, XLV +5.3%, IWM +4.9% | XLI/XLF/XLK | 17.7% | 0.68 | ~0.066 |
| 07-13 | XLF +6.5%, XLV +4.8%, XLI +3.0%, SPY +1.6%, IWM +1.0% | XLF/XLV/XLI | 9.8% | 1.00 | 0.100 |

La cadena input→señal→orden se reproduce exactamente: cada rotación tiene su
causa en el ranking de momentum y el escalar de vol, nada es opaco.

## 3. Operaciones ejecutadas (todas, cuenta paper)

| Fecha fill | Operación | Precio | Resultado |
| --- | --- | --- | --- |
| 07-10 | BUY IWM/XLF/XLV ~$1,000 c/u (señal 07-09) | 297.02/55.91/162.39 | apertura |
| 07-10 | SELL_ALL IWM 3.366 @296.10 (rotación: IWM cayó del top-3) | −$3.1 vs entrada (−0.3%) | cerrada |
| 07-10 | BUY XLI $1,000 @182.31 (entró al top-3) | apertura | |
| 07-13 | SELL_ALL XLV 6.158 @160.55 (salió del top-3 el 07-10) | −$11.3 vs entrada (−1.1%) | cerrada |
| 07-13 | BUY XLK $659 @183.18 (señal 07-10) | apertura | |
| 07-13 | SELL XLF ×2 (¡duplicada!) $674 total @56.06-56.11 | +$1.8 vs entrada | ver §4 |
| 07-13 | SELL XLI ×2 (¡duplicada!) $677 total @181.52 | −$2.9 vs entrada | ver §4 |
| pend. hoy | BUY XLF $671 / XLI $684 / XLV $1,000, SELL_ALL XLK (señal 07-13) | en apertura | |

**Resultado neto a hoy:** equity $99,982.97 vs $99,999.79 inicial = **−$16.8
(−0.017%) en 5 días** — dentro del ruido esperado (la estrategia apunta a ~1%
de vol diaria sobre $20k = ±$60/día típico). PnL realizado de las 2 salidas
completas: IWM −$3.1, XLV −$11.3; costos de ejecución medidos: mediana 15 bps
(incluye gap overnight; cota superior).

## 4. INCIDENTE detectado en esta revisión: ventas parciales duplicadas

**Qué pasó.** El viernes 19:05 la señal recortó XLF/XLI (peso 0.10→0.066 por
el escalar de vol) → órdenes de venta parcial ~$338 c/u, encoladas para el
lunes. Los ciclos de sábado y domingo re-planearon el MISMO recorte (las
posiciones no habían cambiado) y re-emitieron las ventas con ids de su fecha.
El lunes en la apertura **se ejecutaron DOS ventas por símbolo** → XLF y XLI
quedaron en ~$327/318 en vez del objetivo $659.

**Causa raíz.** El fix de exposición pendiente (72895aa) cuenta las COMPRAS
abiertas pero decidió no netear ventas, asumiendo que "una salida duplicada
falla por cantidad en el broker". Esa suposición es cierta para `sell_all`
(la reserva de acciones de Alpaca rechazó los duplicados de XLV y la 3ª venta
del domingo) pero FALSA para ventas PARCIALES: dos parciales caben en la
cantidad disponible y ambas ejecutan.

**Impacto real.** Direccionalmente conservador (menos exposición de la
debida, 3 días), pérdida directa ≈ $0 (las ventas extra se ejecutaron con
PnL ~plano y hoy se recompran); el costo es el spread/gap de ida y vuelta
(~$1-2). Las capas de vigilancia funcionaron: el watch del 07-13 marcó WARN y
la reconciliación etiquetó los huecos como `drift_pending_order`.

**Corrección (Sprint M15, despachado a MiniMax).** Regla simple y simétrica:
si un par tiene CUALQUIER orden sleeve-/breaker- abierta, el ciclo lo trata
como HOLD (`pending_order_hold`) — la orden en vuelo se resuelve primero y el
siguiente ciclo re-planea con posiciones verdaderas. Elimina duplicados de
compra Y de venta con una sola regla.

## 5. Observación de comportamiento (no es bug): churn del viernes

La señal del 07-10 metió XLK y recortó pesos (vol 17.7% > target) y la del
07-13 lo revirtió (XLV re-entró, XLK salió a los 3 días). Este whipsaw es
comportamiento **in-model** (el backtest §28 incluye exactamente este turnover
con sus costos) — se reporta para que el operador vea que la rotación diaria
puede deshacer decisiones recientes en regímenes de vol cambiante; el costo
está dentro de lo asumido (1bp+1bp ETF).
