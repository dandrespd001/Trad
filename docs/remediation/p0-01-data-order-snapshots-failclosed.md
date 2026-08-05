# P0-01 — Datos y snapshots de órdenes fail-closed

## Estado verificado — 2026-07-14

**IMPLEMENTADO Y VERIFICADO EN PAPER/OFFLINE.** Este paquete elimina los tres
fail-open descritos abajo, pero no habilita dinero real ni demuestra
rentabilidad. El programa P0 completo y la evidencia económica P1 siguen
pendientes; `live_trading_allowed` permanece en `false`.

| Superficie | Comportamiento anterior | Contrato actual |
|---|---|---|
| Fetch de mercado | Un símbolo ausente publicaba CSV parcial como `WARN/0` | Cualquier símbolo/barra final ausente bloquea; el CSV previo queda byte-idéntico |
| Publicación | CSV y sidecar se truncaban directamente | Temporales hermanos + `os.replace`; sidecar 1.1 firmado con SHA-256 |
| Universo de rebalance | Ausencia podía convertirse en peso cero y `sell_all` | Universo exacto y barra de decisión común antes de crear el plan |
| Atestación | No se vinculaba el CSV leído con el productor | Provider/feed/rango/recuentos/fechas/hash se verifican y se revalidan antes del submit |
| Órdenes abiertas | Ausencia de método, excepción y vacío eran `{}` | `OK`, `UNAVAILABLE` o `NOT_APPLICABLE`, con fingerprint y segunda lectura |
| Límite Alpaca | La consulta usaba el default de 50 | Solicita 500; llegar al tope se considera snapshot potencialmente incompleto |
| Órdenes externas | Se ignoraban | Se reportan como divergencia y bloquean el batch confirmado |

Archivos de producción modificados por este paquete:

- `src/trading_ai/data/alpaca_market_data.py`
- `src/trading_ai/execution/alpaca_paper.py`
- `src/trading_ai/execution/sleeve_rebalance.py`

Pruebas modificadas o añadidas:

- `tests/test_alpaca_market_data.py`
- `tests/test_alpaca_crypto_market_data.py`
- `tests/test_alpaca_paper_execution.py`
- `tests/test_sleeve_rebalance.py`
- `tests/test_sleeve_circuit_breaker.py`

Evidencia reproducible:

```text
Pruebas dirigidas: 122/122 OK
Regresión completa: 1463/1463 OK en 107.598 s
Ruff crítico E9,F63,F7,F82,F401,B905: OK
py_compile: OK
git diff --check: OK
```

Todas las pruebas fueron offline, con fakes y rutas temporales; no leyeron
credenciales ni llamaron a Alpaca. La API oficial de Alpaca documenta que
`GetOrdersRequest.limit` tiene default 50 y máximo 500:
<https://alpaca.markets/sdks/python/api_reference/trading/requests.html>.

El código de este paquete toca datos financieros, broker y ejecución, por lo
que se mantuvo bajo implementación/revisión directa de Codex y no se envió a
MiniMax. La automatización MiniMax-M3 sigue siendo exclusivamente por API y
solo para código no sensible/delegable.

Compatibilidad intencionalmente rota:

- sidecars 1.0 o sin `source_sha256` ya no habilitan submit confirmado;
- missing/stale cambia de `WARN/0` a `BLOCKED/1`;
- brokers/fakes sin `list_orders` explícito quedan bloqueados;
- órdenes manuales/externas ya no son invisibles.

Riesgo residual: una barra con fecha correcta no demuestra por sí sola que el
proveedor haya cerrado y consolidado la sesión. El scheduler debe solicitar
solo periodos completados; antes de capital real aún faltan watermark de cierre,
journal/idempotencia durable, reconciliación y los demás gates P0/P1.

## Rol del ejecutor

Implementar este paquete de forma acotada. No rediseñar estrategias, no relajar
gates y no activar ninguna ruta live. El resultado debe permanecer paper-only.

## Problemas que corrige

1. Un fetch con símbolos ausentes publica un CSV parcial como `WARN` y devuelve
   éxito operativo.
2. El rebalance valida símbolos permitidos, pero no exige que esté presente el
   universo completo. Un símbolo omitido puede acabar con peso cero y generar
   una venta no intencionada.
3. Si el broker no soporta `list_orders` o la lectura falla, el resumen de
   órdenes abiertas se convierte en `{}`, indistinguible de una lectura válida
   sin órdenes.

## Invariantes obligatorios

- Una lectura incompleta o incierta nunca se interpreta como colección vacía.
- Un dataset parcial nunca sustituye un dataset completo previamente publicado.
- La ausencia de un símbolo nunca puede inferirse como señal para vender.
- Si no se puede probar que datos y snapshot de órdenes están completos, el
  ciclo confirmado termina `BLOCKED`, `orders_submitted=false` y no llama a
  `submit_order`.
- No introducir bypasses, flags de compatibilidad fail-open ni defaults que
  recuperen el comportamiento inseguro.
- No leer credenciales, no llamar a Alpaca y no escribir en `reports/tmp` desde
  tests.

## Cambios requeridos

### 1. Publicación atómica y completa de datos

En `src/trading_ai/data/alpaca_market_data.py`:

- Cambiar `run_market_data_fetch` para que cualquier símbolo configurado sin
  barras produzca `BLOCKED` y exit no-cero.
- No escribir ni truncar el CSV canónico cuando el resultado esté bloqueado.
- Seguir escribiendo el sidecar de diagnóstico con `status=BLOCKED`, símbolos
  ausentes, recuentos y razones.
- Mantener intacto un CSV bueno preexistente ante un fetch parcial posterior.
- Exigir que cada símbolo tenga la barra esperada más reciente según el rango
  solicitado; no basta con que tenga alguna barra antigua.

Aplicar el mismo contrato a equities y crypto.

### 2. Validación del universo completo antes del plan

En `src/trading_ai/execution/sleeve_rebalance.py`:

- Validar OHLCV con `allowed_symbols` y `expected_symbols` iguales al universo
  configurado.
- Antes de calcular pesos, comprobar que todos los símbolos tienen observación
  vigente para el `as_of` aplicable.
- Si falta un símbolo, emitir un blocker estable como
  `dataset_universe_incomplete:<SYMBOL>` y no construir ventas a partir de su
  ausencia.
- Para ciclos con envío confirmado, validar el sidecar `<dataset>.fetch.json`:
  debe existir, ser JSON válido, tener `status=OK`, conjunto exacto de símbolos,
  provider/feed esperado, rango coherente y hash del CSV actual.
- Una atestación ausente, stale, parcial o con hash distinto bloquea el envío.
  El modo puramente report-only puede producir diagnóstico, pero nunca afirmar
  que el ciclo es elegible para submit.

Si el sidecar actual no contiene hash, añadirlo al productor y verificarlo en
el consumidor mediante una función pequeña, testeable y sin dependencia de red.

### 3. Snapshot de órdenes con resultado explícito

En `src/trading_ai/execution/sleeve_rebalance.py`:

- Sustituir el retorno ambiguo de `_open_orders_summary` por un resultado que
  distinga `OK` de `UNAVAILABLE` y contenga el resumen solo cuando la lectura
  sea válida.
- La ausencia de `list_orders`, una excepción, payload inválido o paginación
  incompleta debe ser `UNAVAILABLE`.
- Un ciclo confirmado con snapshot `UNAVAILABLE` debe quedar `BLOCKED` antes de
  cualquier submit.
- Seguir detectando órdenes del propio sistema, pero reportar también órdenes
  externas o símbolos desconocidos como divergencias que bloquean nuevas
  aperturas. No cancelarlas ni modificarlas en este paquete.

## Pruebas requeridas

Actualizar o añadir, como mínimo:

### `tests/test_alpaca_market_data.py`

- `test_symbol_missing_bars_blocks_and_does_not_publish_partial_csv`
- `test_partial_fetch_preserves_previous_good_csv`
- `test_symbol_missing_expected_latest_bar_blocks`

### `tests/test_alpaca_crypto_market_data.py`

- Equivalentes para crypto.

### `tests/test_sleeve_rebalance.py`

- `test_missing_expected_universe_symbol_blocks_without_sell`
- `test_missing_fetch_attestation_blocks_confirmed_cycle`
- `test_non_ok_fetch_attestation_blocks_confirmed_cycle`
- `test_fetch_attestation_hash_mismatch_blocks_confirmed_cycle`
- `test_valid_fetch_attestation_allows_existing_happy_path`
- `test_open_order_read_exception_blocks_without_submission`
- `test_missing_list_orders_capability_blocks_without_submission`
- `test_valid_empty_open_order_snapshot_preserves_happy_path`
- `test_external_open_order_blocks_new_opening_orders`

Los fakes deben declarar explícitamente snapshots exitosos; no conservar un fake
sin `list_orders` que pase por accidente.

## Comandos de verificación autorizados

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv312/bin/python -m unittest \
  tests.test_alpaca_market_data \
  tests.test_alpaca_crypto_market_data \
  tests.test_sleeve_rebalance -v

.venv312/bin/python -m ruff check \
  src/trading_ai/data/alpaca_market_data.py \
  src/trading_ai/execution/sleeve_rebalance.py \
  tests/test_alpaca_market_data.py \
  tests/test_alpaca_crypto_market_data.py \
  tests/test_sleeve_rebalance.py --select E9,F63,F7,F82

git diff --check
git status --short
```

## Entrega esperada

1. Resumen de comportamiento anterior y nuevo.
2. Lista exacta de archivos modificados.
3. Resultado completo de las pruebas dirigidas.
4. Riesgos o compatibilidades rotas identificadas explícitamente.
5. Diff sin commits, sin pushes y sin cambios fuera del repositorio.
