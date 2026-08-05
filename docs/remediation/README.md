# Programa de remediación para trading con capital real

## Estado

El sistema permanece en investigación y paper trading. La existencia de código
`live_*` no implica preparación para capital real. `live_trading_allowed` debe
seguir deshabilitado hasta que todos los gates P0 y la campaña P1 estén
demostrados con evidencia actual y reproducible.

La reauditoría de la frontera live del 2026-07-14 mantiene un veredicto
**NO-GO** y deshabilita por código tanto el submit del canary como el del
adaptador Alpaca, incluso si un caller pasa flags de activación. Véase
[Bloqueo de frontera live](p0-live-boundary-no-go-2026-07-14.md).

Este programa no promete rentabilidad. Su objetivo es eliminar evidencia
inflada, limitar pérdidas operativas y determinar honestamente si existe edge
neto después de costes.

## Roles de trabajo

- **MiniMax-M3 API / ejecutor acotado:** recibe únicamente un spec y archivos
  no sensibles allowlisted/escaneados, no tiene tools y propone un parche sobre
  un snapshot aislado. Nunca se ejecuta MiniMax localmente.
- **Codex / arquitecto y supervisor:** define invariantes, revisa cada diff,
  ejecuta verificaciones independientes y rechaza cambios fail-open.
- **Gates deterministas:** tienen la última palabra sobre promoción. Ningún LLM
  puede aprobarse a sí mismo ni enviar órdenes.

La delegación automática v0.3.3 usa la Responses API oficial de MiniMax-M3 con
una clave Token Plan `sk-cp-`. La autorización permanente ya concedida permite
jobs acotados no sensibles sin aprobación humana por cada ejecución; el flag
`--cloud-approved` registra esa autorización, pero no evita la política de red o
egress de la plataforma. La credencial vigente se lee desde Fish —no zsh—, pero
nunca se entrega al modelo, a Git ni al checkout. El
wrapper histórico de Claude Code queda fuera: Codex llama al worker directo con
`/usr/bin/python3 -I`, conserva la dirección y revisa el patch determinista antes
de aplicarlo. Estado y exports quedan confinados a
`/tmp/minimax-api-worker-$UID`; el POST no se reintenta y existe un solo slot de
jobs Responses por usuario. El DLP es best-effort, no una garantía de ausencia de
secretos. Un canario API real con código sintético no sensible terminó
`PATCH_READY`, fue revisado/probado y se purgó. Un intento posterior con un
bundle privado mínimo terminó en un fallo de transporte ambiguo, sin respuesta,
usage ni patch; la entrega no se puede confirmar, el job se purgó y no se
reintentará.
`live_trading_allowed` continúa en `false` y esta integración no promete
rentabilidad. El diseño y operación
están en [Integración autónoma Codex → MiniMax-M3](minimax-codex-autonomy.md).
La Subscription Key puede consumir Credits comprados tras la cuota incluida;
evitar ese gasto requiere control externo de cuenta/cuota porque Responses no
ofrece un switch por request.

El inventario global de Codex mantiene 9 MCPs habilitados y 6 plugins con 57
Skills. La reducción de privilegios, fijación de versiones y eliminación de
duplicados propuestas —sin modificar todavía la configuración global— están en
[Auditoría Codex: MCP, Skills y Plugins](codex-mcp-skills-plugins-audit.md).

## Orden de paquetes

### P0 — corregir seguridad y evidencia

1. [P0-01 — Datos y snapshots de órdenes fail-closed](p0-01-data-order-snapshots-failclosed.md) — implementación paper/offline verificada; no habilita live.
2. [P0-02 — Journal durable, reducción y flatten reconciliado](p0-02-order-journal-reducing-flatten.md) — implementación paper/offline verificada; live sigue deshabilitado.
3. [P0-03 — Paridad temporal del backtest, costes, calendario y cash por sleeve](p0-03-backtest-causality-cost-calendar.md) — implementación v2 verificada; evidencia económica aún invalidada.
4. [P0-04 — Gate 1, costes firmados y ledger de evidencia](p0-04-gate1-signed-costs-evidence-ledger.md) — núcleo paper/offline verificado; reconciliación económica live aún incompleta.
5. [P0-05 — Autoridad de cuenta paper](p0-05-account-authority-partial.md) —
   executor, epoch SQLite, authz cross-UID y paquete systemd implementados en el
   repositorio; el inventario productivo ya tiene cero consumidores directos y
   existen probes `SIGKILL` y cross-UID reales. Aún faltan instalación/cutover,
   callers systemd, riesgo server-side y la campaña integral de fallos/paper.
   El launcher MCP heredado está deshabilitado.
6. P0-06 — Configuración, secretos, estado durable y CI aislado.
7. P0-07 — Gate de release y fault injection integral.

### P1 — demostrar valor económico

1. Datos y universos point-in-time; paridad de feeds de investigación/broker.
2. Ledger de experimentos, splits purgados, DSR con todos los trials y
   bootstrap por bloques.
3. Baselines deterministas y challengers de IA desacoplados en shadow.
4. Campaña paper congelada de duración y muestra prerregistradas.
5. Shadow live sin órdenes, seguido —solo con aprobación humana independiente—
   de un canary mínimo y reversible.

## Flujo obligatorio de cada paquete

```text
spec aprobado por Codex
  -> [solo si el paquete es delegable] snapshot mínimo + DLP + manifest de egress
  -> [solo si es delegable] parche MiniMax-M3 API sin tools
  -> validación local + revisión de diff Codex
  -> pruebas dirigidas
  -> pruebas de regresión/release
  -> evidencia y limitaciones
```

La delegación a MiniMax es automática cuando el alcance es delegable, pero no
es obligatoria ni puede usarse para controles financieros, datos de mercado,
broker, ejecución, riesgo, promoción, secretos o deploy. Esos paquetes quedan
bajo implementación y revisión directa de Codex; omitir MiniMax allí es una
frontera de seguridad, no una intervención manual del operador.

No se aceptan cambios que:

- reduzcan o eliminen una prueba para conseguir verde;
- transformen `UNKNOWN`, timeout o lectura incompleta en éxito/vacío;
- relajen un umbral después de observar el resultado;
- mezclen test state con estado operativo;
- habiliten live, lean credenciales o ejecuten órdenes;
- atribuyan rentabilidad a paper sin modelar sus limitaciones.

## Definición de P0 completo

- Una única autoridad de escritura por cuenta y lock con fencing.
- Journal durable e idempotencia broker-first ante reinicios y timeout.
- Datos, cuenta, posiciones y órdenes completas o ciclo bloqueado.
- Breaker y flatten reducen exposición y solo declaran éxito tras fills
  terminales y reconciliación.
- Backtest usa precios realmente disponibles y mantiene cash cuando una sleeve
  está cerrada.
- Costes separados en fee, spread, gap, latencia e implementation shortfall
  firmado.
- Todos los artefactos anteriores afectados quedan invalidados y regenerados.
- Suite de release verde sin modificar estado operativo.
- Live continúa deshabilitado.

## Definición de evidencia económica suficiente

Los umbrales deben prerregistrarse antes de ejecutar un holdout nuevo. Como
mínimo, la evidencia debe incluir benchmark con riesgo comparable, retorno neto
de costes p90, drawdown, exposición, turnover, capacidad, concentración de PnL,
intervalos de confianza, DSR/PBO con todos los experimentos y estabilidad por
régimen. Si el challenger no mejora al baseline en los mismos folds, costes y
universo, queda fuera del camino operativo.

## Referencias técnicas adoptadas

- Alpaca documenta que paper no modela impacto, latencia, posición en cola,
  price improvement, fees regulatorios ni liquidez real:
  <https://docs.alpaca.markets/us/docs/paper-trading>.
- Alpaca recomienda mantener estado por streaming y permite recuperar una orden
  por `client_order_id`:
  <https://docs.alpaca.markets/us/docs/orders-at-alpaca>.
- NautilusTrader separa estados activos, reducción y halt, además de reconciliar
  órdenes/posiciones:
  <https://nautilustrader.io/docs/latest/concepts/execution/>.
- Backtrader establece que una market basada en una barra cerrada se ejecuta en
  la apertura de la siguiente barra:
  <https://www.backtrader.com/docu/order-creation-execution/order-creation-execution/>.
- Qlib mantiene separados forecast, estrategia, ejecución y workflow, patrón
  que se adopta para aislar IA de ejecución:
  <https://github.com/microsoft/qlib>.
- Bailey y López de Prado describen la corrección del Sharpe por selección de
  múltiples trials:
  <https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf>.
