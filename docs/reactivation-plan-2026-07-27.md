# Plan de reactivación y mejora económica — 2026-07-27

## Decisión ejecutiva

El proyecto está en **recuperación controlada**.

- **GO**: auditoría de solo lectura, saneamiento del checkout, reproducibilidad,
  pruebas aisladas, investigación offline y observabilidad paper fail-closed.
- **HOLD**: aperturas y rebalanceos paper hasta completar P0-05 con riesgo
  durable server-side y reconciliación verificable.
- **NO-GO**: capital real, canary live, promoción automática o aumento de
  exposición.

La meta económica no es maximizar una cifra histórica. Es demostrar retorno
activo neto y ajustado por riesgo, fuera de muestra, bajo el contrato causal
`next_open_v2`, con costes conservadores y corrección por múltiples pruebas.
No se promete rentabilidad.

## Orquestación realizada

| Frente | Modelo disponible | Pensamiento | Encargo | Resultado |
|---|---|---:|---|---|
| Arquitectura y recuperación | GPT-5.6 Sol | xhigh | Checkout, gates, reproducibilidad y orden crítico | Auditoría lanzada; no se autorizó escritura |
| Evidencia económica | GPT-5.6 Tierra | high | Backtest causal, costes, OOS, DSR y bootstrap | Secuencia experimental definida |
| Operación paper y seguridad | GPT-5.6 Tierra | medium | P0-05, IPC, systemd, reconciliación y rollback | Reactivación limitada a observación fail-closed |
| Supervisión y decisión final | Codex | high | Integración, límites y verificación | Este plan |

GPT-5.6 Luna y Codex 5.3 Spark no están expuestos por el runtime de agentes de
esta sesión. No se sustituyeron de forma encubierta ni se atribuyó trabajo a
modelos no disponibles.

## Restricción de delegación MiniMax

La tarea de bootstrap estadístico era, por alcance, elegible para delegación
acotada. Sin embargo, el bridge lock del repositorio declara:

```text
installation.status = HOLD
reason = active global runner is v0.3.2 and has no verifier
```

Además, los hashes del runner global no coinciden con el worker y verificador
sellados por el repositorio. Conforme a `AGENTS.md`, no se creó ningún job ni
se envió código o datos al proveedor. Hasta instalar una versión exacta con
estado `READY`, Codex debe trabajar directamente y registrar esta exclusión.

## Estado que impide reactivar aperturas

1. El checkout contiene una divergencia extraordinaria: 98 archivos
   modificados y múltiples módulos, tests y paquetes operativos sin seguimiento.
   No existe todavía una unidad revisable ni un punto de rollback fiable.
2. La suite completa ejecutada desde el checkout puede escribir artefactos
   operativos ignorados, incluido el estado de riesgo paper. Las pruebas de
   release deben ejecutarse en una copia o sandbox aislado.
3. P0-05 está implementado parcialmente, sin cutover. El executor empaquetado
   arranca en `reduce_only`: no puede abrir posiciones de forma segura.
4. El estado paper real requiere reconciliación humana read-only contra broker,
   journal, órdenes y posiciones. No se debe reconstruir desde artefactos de
   prueba.
5. P0-03 corrigió causalidad, costes y cash, pero invalidó la evidencia económica
   anterior. Aún no existe edge promovible bajo el motor corregido.
6. P0-04 conserva brechas de atribución económica: quotes de decisión/envío/fill,
   fees finales, busts y shortfall no están completamente conciliados.
7. El timer matinal continúa activo: el 2026-07-27 a las 08:45 volvió a fallar
   porque la identidad del executor paper no está provisionada. No produjo una
   reactivación; sólo confirmó el mismo bloqueo operativo.

Actualización 08:56: `trading-n0.timer`, `trading-crypto-sleeve.timer` y
`trading-position-watch.timer` fueron deshabilitados y detenidos de forma
reversible. Los tres quedaron `disabled` e `inactive`. Esta contención no
modificó posiciones ni contactó al broker.

## Camino crítico

### R0 — Contención y recuperación del checkout

Responsable: Codex + GPT-5.6 Sol.

1. Capturar un backup recuperable de commits y working tree sin incluir
   credenciales, datasets ni reportes operativos.
2. Separar el trabajo en cambios revisables:
   - motor P0-03 y tests;
   - ledger/costes P0-04;
   - executor P0-05;
   - systemd/deploy;
   - documentación;
   - experimentos estadísticos.
3. No mezclar saneamiento de lint/tipos con cambios económicos o de ejecución.

Criterio de salida: cada paquete tiene diff acotado, inventario de archivos,
tests focales, rollback y propietario.

### R1 — Release reproducible y pruebas aisladas

Responsable: Codex. MiniMax queda excluido mientras el bridge esté en `HOLD`.

1. Fijar un único Python 3.12 y un lock verificable de dependencias.
2. Ejecutar full suite y coverage desde una copia aislada; el checkout y
   `reports/tmp` deben permanecer byte-idénticos.
3. Ejecutar las pruebas AF_UNIX/IPC en un sandbox que permita sockets locales
   pero niegue red externa y credenciales.
4. Resolver primero los errores del scope mypy de release y Ruff crítico; la
   deuda global se divide en paquetes posteriores.

Criterio de salida:

- suite canónica verde;
- cobertura al menos 75%;
- Ruff crítico, mypy de release, scanners live/futures y Bandit verdes;
- `git diff --check` verde;
- cero escrituras de pruebas en artefactos operativos.

Avance inicial: `scripts/run-tests-isolated.sh` crea una copia temporal sin
`.env`, datasets, reportes operativos ni entornos virtuales. La verificación
focal del bootstrap pasó 9/9 dentro de esa copia. Ninguna suite debe ejecutarse
desde el checkout real.

Actualización R1:

- 1.951 pruebas no-IPC pasaron dentro de la copia aislada tras R4;
- 29 pruebas IPC/AF_UNIX pasaron fuera del sandbox;
- total combinado: 1.980 pruebas aprobadas y 36 omitidas;
- contenido y mtime de `reports/tmp` permanecieron idénticos;
- el runner propaga el Python 3.12 resuelto y conserva una copia temporal de
  `.git` para validar los gates sin tocar el checkout original.

La cobertura, Ruff global, mypy global y auditoría de dependencias con red
siguen siendo gates separados; la suite verde no los sustituye.

Actualización de gates:

- coverage 7.15.2 instalada en `.venv312`;
- cobertura combinada no-IPC + IPC: **85%**, gate 75% superado;
- cobertura focal de los módulos modificados en R4: **78%**, gate 75%
  superado (`oos_evaluation.py`: 94%);
- Ruff crítico: verde;
- mypy del scope de release: verde después de corregir un estrechamiento de
  tipos en el gate de evidencia de señales LLM;
- `pip check`: sin requisitos rotos.

Ruff/mypy globales y la auditoría de vulnerabilidades de dependencias siguen
pendientes; no se confunden con el scope de release.

### R2 — Reactivación observacional paper

Responsable: operador humano + Codex, sin mutaciones de broker.

1. Mantener `live_trading_allowed=false` y aperturas bloqueadas.
2. Obtener un snapshot paper read-only autorizado fuera de este proceso.
3. Reconciliar posiciones, órdenes abiertas, fills, journal y estado de riesgo.
4. Regenerar, con una única fecha ISO, position watch, plan EOD, monitor,
   performance, ops check e índice de evidencia.
5. Ante cualquier estado stale, discrepancia, `CRITICAL`, `ERROR`, checksum
   inválido o kill switch activo, conservar `BLOCKED`.

Criterio de salida: observabilidad coherente y reconciliación con divergencia
cero. Este gate no autoriza nuevas aperturas.

### R3 — Cierre de P0-05

Responsable: Codex directo. Excluido de MiniMax por tocar ejecución, IPC,
credenciales y riesgo.

1. Proveer riesgo durable server-side antes de aceptar aperturas.
2. Validar single-writer y fencing por cuenta.
3. Instalar un release inmutable en staging con identidades separadas,
   autorización default-deny y credenciales entregadas únicamente al daemon.
4. Probar restart, SIGKILL, recuperación de journal, multi-UID, idempotencia,
   órdenes ambiguas y reconciliación.
5. Mantener `open` fuera de la policy hasta que todos los gates sean verdes.

Criterio de salida: executor `READY`, sin credenciales fuera del daemon, riesgo
server-side válido y recuperación determinista ante fallos.

### R4 — Reconstrucción de evidencia económica P0-03/P0-04

Responsable: GPT-5.6 Tierra propone; Codex ejecuta y decide.

Prerregistrar antes de ver resultados:

- universo y benchmark de cada sleeve;
- train, validation y holdout OOS;
- parámetros y semilla;
- costes base y escenarios 1x, 1.5x y 2x;
- número total de trials, incluidos fallidos;
- métricas y umbrales de rechazo.

Métricas mínimas:

- retorno activo neto OOS;
- CAGR, Sharpe, Sortino y Calmar;
- drawdown máximo;
- exposición y turnover;
- coste y break-even cost por sleeve;
- estabilidad por fold y régimen;
- DSR/PSR usando el número y varianza reales de trials.

Rechazo automático:

- retorno activo neto OOS no positivo;
- edge ausente bajo coste conservador prerregistrado;
- ventaja concentrada en un único fold;
- ledger de trials incompleto o reconstruido retrospectivamente;
- fallo de causalidad, calendario, cash o doble cobro de costes.

Avance R4:

- el protocolo combinado continúa `DRAFT_BLOCKED`, pero su fase ETF técnica
  quedó `PREREGISTERED_TECHNICAL_ONLY`;
- hashes de código, universo, riesgo, fuente, snapshot, splits, benchmark,
  costes, semilla y bootstrap quedaron congelados antes del ensayo;
- el snapshot ETF operativo sólo cubre 2026-03-26 a 2026-07-24 y no basta para
  cuatro folds;
- no existe snapshot cripto aprobado;
- se preparó en `/tmp`, sin reemplazar datos operativos, un candidato ETF
  estructuralmente válido de 6.200 filas para 2024-01-02 a 2026-06-23;
- el motor ahora admite una frontera `evaluation_start` exacta: conserva
  historial causal para indicadores, reinicia en cash y prohíbe decisiones o
  posiciones anteriores al OOS;
- se añadió evaluación OOS pura, benchmark SPY next-open y bootstrap circular
  de pares estrategia/benchmark, sin I/O, red, broker, CLI ni imports de
  ejecución/riesgo;
- el ensayo técnico `p0-03-etf-technical-fd8b11a8dc0c` fue registrado antes de
  ejecutarse y quedó encadenado en `docs/research-trials/ledger.jsonl`;
- resultado 1x: estrategia +0,80%, SPY +6,83%, retorno activo compuesto
  **-5,64%**, Sharpe activo **-0,96**, bootstrap p05 **-20,49%** y sólo 30,72%
  de réplicas con retorno activo positivo;
- 1,5x y 2x también tuvieron retorno activo negativo; dos de cuatro folds
  fueron negativos;
- decisión: `REJECTED_RESEARCH_CANDIDATE` y clasificación obligatoria
  `EXPLORATORY_INCONCLUSIVE_SHORT_OOS`.

La estrategia mantuvo una exposición media de sólo 3,87%. Ese dato identifica
un problema de utilización de capital, pero no autoriza subir exposición de
forma post hoc: el OOS ya fue observado. Cualquier variante de sizing debe
prerregistrarse, contarse como nuevo trial y evaluarse en datos futuros no
vistos. La siguiente evidencia económica requiere al menos 252 observaciones
OOS y 60 por fold.

Actualización de campaña de sizing:

- se prerregistraron y reservaron seis variantes antes de ejecutarlas, con
  18 corridas para costes 1x, 1,5x y 2x;
- la selección usó exclusivamente 228 sesiones de 2025; el intervalo
  retrospectivo de 2026 no fue leído ni utilizado;
- la variante T05 elevó la exposición media a 14,49% y mejoró el retorno activo
  frente a T01 en todos los escenarios de costes, pero en 1x sólo alcanzó CAGR
  1,12%, Sharpe 0,57, bootstrap activo p05 -1,20% y DSR 0,388;
- todas las variantes continuaron por debajo de SPY por más de 13% en el
  intervalo de validación;
- decisión sellada: `NO_CANDIDATE`. No se amplía la cuadrícula, no se crea
  challenger forward y no cambia ninguna configuración paper, de riesgo o live.

La conclusión arquitectónica es que aumentar sizing amplifica la señal
existente, pero no crea edge. El próximo ciclo económico debe investigar una
hipótesis de señal distinta y prerregistrada sobre una fuente homogénea, o
acumular un historial IEX independiente cuyo warmup y retornos puntuados sean
enteramente IEX.

Verificación de cierre:

- 49 pruebas focales de selector, evaluación OOS, bootstrap y motor causal
  pasaron en un checkout aislado;
- Ruff, mypy del nuevo scope, `pip check` y `git diff --check` quedaron verdes;
- los cuatro eventos del ledger fueron verificados contra sus hashes de archivo,
  hashes canónicos y enlaces previos;
- respaldo recuperable posterior al cierre:
  `.recovery/2026-07-27-1509/`, con bundle Git verificado y checksums íntegros.

Actualización de siguiente ciclo — señal y procedencia:

- Sol xhigh auditó la arquitectura de campaña; Tierra high diseñó la hipótesis
  y Tierra medium auditó las fuentes sin leer precios ni retornos;
- una campaña nueva no puede tratar 2024/2025 como evidencia confirmatoria:
  el repositorio documenta búsquedas previas de parámetros, régimen, modelos y
  features sin un registry histórico completo;
- se congeló como borrador una familia de cinco señales, manteniendo sizing,
  universo, riesgo y `next_open_v2` idénticos;
- antes de ejecutarla se requiere un benchmark SPY con exposición igualada y
  una fuente IEX homogénea con warmup, train, validation y forward bajo el mismo
  contrato;
- el fetch de equities ahora fija `DataFeed.IEX`, `Adjustment.ALL`, UTC, XNYS,
  versión del SDK y hashes; el sidecar 1.1 conserva compatibilidad operativa y
  añade el contrato IEX `iex-1.0`;
- la regeneración histórica quedó `HOLD`: este proceso no dispone de
  credenciales read-only de Alpaca Market Data. El intento falló antes de
  construir un cliente y no creó ni sustituyó datasets.

Actualización del gate de calendario y benchmark:

- el snapshot XNYS de cierres completos 2024–2028 quedó congelado como
  `xnys-full-day-2024-2028-v2`, con SHA-256
  `886eed762ac68ac5e90520d79a3b67b0c09a93c8aa2f4a69bedf434a7bf7ceb0`
  y hash separado de implementación
  `a7a8ecef40f9285849895a7780d2b8033af4993eac308efbb039dff06ee18dea`;
- productor, importador y consumidor exigen el mismo hash, `generated_at`
  timezone-aware y un watermark de sesión cerrada recalculado; clientes o
  relojes inyectados, rangos futuros, drift o atestaciones incompletas fallan
  antes de contactar al proveedor o crear resultados;
- los fines de rango en fin de semana o feriado se comparan contra la última
  sesión XNYS gobernada, no contra el literal `end`;
- el paquete IEX aprobado retiene copias locales inmutables de CSV y sidecar;
  evaluación, model research y benchmark comparten un único loader que vuelve
  a verificar digests, SDK, configuración, normalizador, causalidad y conjuntos
  exactos de sesiones antes de crear resultados;
- los consumidores genéricos todavía admiten `manual_csv` para investigación,
  pero eso no autoriza esta campaña; el runner target-schedule pendiente deberá
  exigir `required_provider=alpaca_market_data`;
- el núcleo puro `SPY_EXPOSURE_MATCHED_NEXT_OPEN_V1` ya está implementado y
  probado; su alineación todavía depende del runner genérico `next_open_v2`;
- 104 pruebas focales de calendario, fetch, importación, consumo, model
  research y benchmark pasaron en el
  entorno Python 3.12 del repositorio, junto con Ruff, mypy del scope nuevo y
  `git diff --check`;
- la decisión continúa `HOLD`: faltan una fuente IEX real y el runner
  target-schedule; nada de esto habilita órdenes ni cambia
  `live_trading_allowed=false`.

Artefactos:

- `docs/data-contract-iex-signal-campaign-v1.md`;
- `docs/preregistration-etf-signal-campaign-v1.yml`.

El siguiente gate humano no es financiero ni autoriza órdenes: provisionar
temporalmente las dos variables de market data para regenerar un CSV IEX nuevo,
o ejecutar externamente el comando documentado y entregar CSV + sidecar
1.1/`iex-1.0`.

### R5 — Robustez y mejora, sólo después de R4

1. Walk-forward de PnL, no sólo accuracy.
2. Bootstrap circular por bloques sobre retornos OOS para intervalos de retorno,
   Sharpe y drawdown p95/p99.
3. Evaluar filtro de régimen como challenger opt-in.
4. Evaluar diversificación únicamente por contribución marginal neta a retorno,
   drawdown y estabilidad; no por Sharpe in-sample aislado.

La primitiva `iter_circular_block_bootstrap_indices` fue verificada con nueve
pruebas focales. Aún no debe conectarse a promoción: faltan el contrato
estadístico prerregistrado y las métricas económicas.

## Backlog priorizado

| Orden | Trabajo | Impacto | Riesgo | Gate |
|---:|---|---:|---:|---|
| 1 | Backup y segmentación del working tree | Muy alto | Bajo | R0 |
| 2 | Aislar suite de artefactos operativos | Muy alto | Bajo | R1 |
| 3 | Fijar runtime/lock Python 3.12 | Alto | Bajo | R1 |
| 4 | Reconciliar estado paper read-only | Muy alto | Medio | R2 |
| 5 | Completar riesgo server-side y P0-05 | Muy alto | Alto | R3 |
| 6 | Prerregistrar y regenerar P0-03 | Muy alto | Medio | R4 |
| 7 | Sensibilidad de costes + trial ledger/DSR | Muy alto | Medio | R4 |
| 8 | Walk-forward + bootstrap económico | Alto | Medio | R5 |
| 9 | Filtro de régimen/diversificación | Medio | Medio | R5 |
| 10 | Campaña paper representativa | Muy alto | Alto | R0-R5 verdes |

## Regla de promoción

Ningún resultado de investigación habilita por sí solo órdenes, paper con
aperturas o live. La promoción requiere, de forma acumulativa:

1. release reproducible y suite aislada verde;
2. P0-03/P0-04 revalidados con evidencia nueva;
3. P0-05 cerrado y executor seguro;
4. reconciliación operativa sin divergencias;
5. campaña paper prerregistrada y limpia;
6. revisión humana separada.

`live_trading_allowed` permanece `false`.
