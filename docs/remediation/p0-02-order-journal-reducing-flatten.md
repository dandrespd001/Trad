# P0-02 — Journal durable, reducción y flatten reconciliado

## Estado y alcance

**Estado técnico:** verificado para paper/offline el 2026-07-14. La evidencia
reproducible está en
[evidence-p0-02-2026-07-14.md](evidence-p0-02-2026-07-14.md).

Este paquete protege exclusivamente paper trading y preparación offline. No
autoriza capital real, no prueba rentabilidad y no habilita ninguna ruta live.
`configs/risk.yml` debe conservar `live_trading_allowed: false`.

La auditoría inicial encontró que el adapter conservaba idempotencia solo en memoria,
reintenta un `POST` después de timeouts ambiguos y declara cancelación o flatten
al recibir una aceptación. También omitía posiciones cortas y posiciones fuera
del allowlist durante un flatten. La implementación verificada de este paquete
corrige esos comportamientos fail-open en el camino paper.

El código de este paquete es control financiero y de ejecución. Codex lo diseña,
implementa, revisa y prueba directamente; no se envía a MiniMax-M3.

## Invariantes

### 1. Write-ahead journal

- Todo submit paper no simulado requiere un journal SQLite accesible y sano.
- La intención se confirma en disco antes del primer `POST` al bróker.
- La clave funcional es `client_order_id`; un SHA-256 enlaza de forma inmutable
  símbolo, lado, cantidad o notional, tipo, TIF e intención de posición.
- Mismo ID y mismo fingerprint es replay. Mismo ID e intención distinta es una
  divergencia bloqueante y nunca toca al bróker.
- El historial de transiciones es append-only. Un fallo de apertura, escritura,
  lock o integridad del journal bloquea antes de enviar.
- La identidad de orden del bróker es única en todo el journal. Una misma
  intención observada con dos IDs del bróker, o dos intenciones enlazadas al
  mismo ID, es una divergencia bloqueante incluso si el estado no cambió.
- El proceso de flatten fija al inicio la identidad del archivo SQLite
  (dispositivo/inodo) y su metadata durable. Borrarlo o sustituirlo por otro
  SQLite válido durante la secuencia bloquea; nunca se recrea y acepta como un
  journal vacío en la auditoría final.
- Cada lectura vuelve a validar que la fila materializada coincide con toda su
  cadena de eventos; modificar directamente estado, ID o historial invalida el
  journal completo.
- La garantía es *effectively once*: ningún sistema distribuido puede prometer
  exactly-once entre el disco local y el bróker.

### 2. Recuperación broker-first

- Una intención recuperada después de reinicio se consulta primero por el mismo
  `client_order_id`.
- Si existe y coincide con la intención, se adopta sin repetir el `POST`.
- Si el lookup falla, expira o no distingue ausencia de indisponibilidad, el
  estado es `SUBMIT_UNRESOLVED`; no se reenvía.
- Un timeout, desconexión, 408, 429 o 5xx después de iniciar el submit produce
  `SUBMIT_UNRESOLVED`; la única acción automática permitida es reconciliación
  read-only.
- Una orden recuperada con símbolo, lado, tipo, TIF, cantidad/notional o precio
  límite incompatibles produce divergencia bloqueante.

### 3. Estados conservadores

Estados no terminales como `accepted`, `new`, `pending_new`,
`partially_filled`, `pending_cancel`, `pending_replace`, `done_for_day`,
`stopped`, `suspended` y `calculated` nunca equivalen a cierre.

Los terminales son `filled`, `canceled`, `expired` y `rejected`. Una orden
`replaced` obliga a seguir la cadena de reemplazo; mientras esa cadena no esté
implementada y validada, el adapter la bloquea explícitamente en vez de
adivinar. Los fills parciales se conservan incluso si la orden termina
cancelada.

### 4. Cancelación

- La aceptación de un cancel solo registra `CANCEL_REQUESTED`.
- No se repite un cancel mientras el bróker confirme `pending_cancel`, ni cuando
  la orden ya sea terminal.
- Después de un crash o DELETE ambiguo, una lectura que confirme que la orden
  sigue abierta permite reintentar el cancel como acción de reducción de riesgo;
  la ambigüedad por sí sola nunca autoriza ese segundo DELETE.
- Solo una consulta o evento terminal permite registrar `CANCELED`.
- Un cancel ambiguo impide cualquier fallback market porque la orden anterior
  todavía puede llenar.

### 5. Reducción local

Alpaca no expone un `reduce_only` general para equity y crypto. Se impone de
forma local:

- la orden expresa `OPEN`, `INCREASE`, `REDUCE` o `CLOSE`;
- `REDUCE/CLOSE` usa cantidad, nunca notional;
- una posición long solo se reduce con `sell` y una short solo con `buy`;
- la cantidad es como máximo la exposición disponible después de descontar
  cierres ya abiertos;
- snapshot incompleto, posición cero, carrera observada o cruce potencial de
  cero bloquean;
- se releen posiciones y órdenes inmediatamente antes de enviar.

Las respuestas de cuenta, posiciones y órdenes se validan como contratos: los
campos de identidad y routing son obligatorios, todos los números deben ser
finitos y consistentes, no se aceptan símbolos duplicados ni cantidades llenadas
mayores a las ordenadas, y alcanzar el límite de 500 órdenes se considera un
snapshot potencialmente truncado.

P0-05 añadirá el single-writer con fencing de cuenta. Hasta entonces este
paquete reduce, pero no elimina, la carrera entre procesos independientes.

### 6. Flatten seguro

La secuencia obligatoria es:

```text
kill switch latched
  -> snapshot completo de posiciones y órdenes
  -> cancelar órdenes abiertas
  -> confirmar cancelaciones terminales
  -> releer posiciones y órdenes
  -> enviar cierres locales reduce-only, de forma serial
  -> esperar cada estado terminal
  -> releer posiciones y órdenes
  -> reconciliar journal
  -> FLAT o FAILED_LATCHED
```

`FLAT` exige simultáneamente:

- cero posiciones de cualquier símbolo de la cuenta;
- cero órdenes abiertas que puedan modificar exposición;
- todos los cierres terminales y reconciliados;
- auditoría completa del journal durable sin estados ambiguos o no terminales,
  no solo de las órdenes creadas durante la ejecución actual.

El kill switch se persiste antes de la primera lectura o mutación del bróker y
solo puede resetearse después de esa evidencia. Cualquier error mantiene el
latch activo y devuelve un artefacto `ERROR` redacted. Los estados terminales
históricos se reconcilian automáticamente únicamente después de un snapshot
completo con cero posiciones y cero órdenes abiertas; los ambiguos nunca se
avanzan. La evidencia se persiste antes del reset y un fallo posterior intenta
re-latchear de forma durable.

## Pruebas negativas obligatorias

- timeout de submit + lookup indisponible: un solo `POST`, estado ambiguo;
- reinicio + orden encontrada: cero `POST` adicionales;
- colisión de intención: cero llamadas al bróker;
- journal ausente/corrupto/no escribible: cero llamadas al bróker;
- cancel aceptado o `pending_cancel`: no market fallback;
- sell sin long, buy sin short o cantidad excesiva: bloqueados;
- close `accepted` o parcial: no apertura posterior, no `FLAT`;
- orden externa, posición fuera del allowlist o snapshot incompleto: bloqueados;
- journal previo `SUBMIT_UNRESOLVED`, `CANCEL_UNRESOLVED` o no terminal: no
  `FLAT`, no reset del kill switch aunque el bróker devuelva listas vacías;
- borrado o reemplazo del journal durante la secuencia: `ERROR` y latch activo;
- fallo al persistir la evidencia de `FLAT`: no reset o re-latch inmediato;
- identidad de bróker divergente con el mismo estado: bloqueada;
- excepción con token/clave en el texto: artefacto `ERROR` sin el secreto;
- solo fill terminal + posiciones cero + órdenes cero permite reset del breaker.

## Evidencia de cierre

El gate se cerró el 2026-07-14 con 1533/1533 pruebas de regresión, 252/252
pruebas dirigidas y una reauditoría independiente de 223/223 casos. Ruff,
compilación y `git diff --check` pasaron. El artefacto enlazado registra comandos,
reproducciones negativas, hashes y limitaciones. `live_trading_allowed`
continúa en `false`.

## Fuentes primarias

- [Alpaca: ciclo de vida y estados de órdenes](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
- [Alpaca-py: consulta, cancelación y creación de órdenes](https://alpaca.markets/sdks/python/api_reference/trading/orders.html)
- [Alpaca-py: cierre de posiciones](https://alpaca.markets/sdks/python/api_reference/trading/positions.html)
- [Alpaca-py: `position_intent` y requests](https://alpaca.markets/sdks/python/api_reference/trading/requests.html)
- [NautilusTrader: estados terminales y no terminales](https://nautilustrader.io/docs/latest/concepts/orders/)
- [NautilusTrader: reconciliación de ejecución](https://nautilustrader.io/docs/latest/concepts/execution/)
