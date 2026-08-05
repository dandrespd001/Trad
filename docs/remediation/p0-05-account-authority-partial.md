# P0-05 — Autoridad única de cuenta paper (implementada, sin cutover)

Fecha de revisión: 2026-07-15.

## Veredicto

Estado: **PARTIAL en el repositorio / NO-GO para operar dinero real**.

La frontera de executor único ya existe en código, pero no está instalada ni
conectada a los timers activos. En la arquitectura empaquetada, el daemon es el
único componente que posee credenciales, SDK, lease vitalicio, journal de
órdenes y ledger de comandos. Los callers disponen de un cliente IPC de DTOs
cerrados que no expone SDK, credenciales, HTTP arbitrario ni reset online del
kill switch. Esa exclusividad todavía no describe al sistema desplegado.

El transporte es AF_UNIX/JSON estricto y con `SO_PEERCRED`. Lectores acotados
validan un frame por UID, mientras un único dispatcher ejecuta la aplicación y
prioriza mutaciones ya listas sobre observaciones; cada mutación queda ligada a
cuenta, policy hash, run ID y fence epoch. La frontera cross-UID usa un usuario
de daemon fijo, directorio `0750`, socket `0660`, grupo IPC dedicado y
autorización default-deny UID→capabilities. El daemon arranca
deliberadamente en capacidad `reduce_only`: sin un proveedor de riesgo
server-side no acepta aperturas. Esta implementación reduce el riesgo técnico,
pero aún no prueba autoridad efectiva mientras el servicio no esté instalado y
los timers/procesos operativos no hayan hecho cutover a la release auditada.

MiniMax-M3 no recibió este paquete. En este sistema MiniMax se usa únicamente
por la API remota Token Plan, nunca como modelo local, y queda fuera de cuenta,
journal, broker, ejecución, riesgo, fencing, promoción y datos financieros. Esos
controles protegidos fueron implementados y revisados directamente por Codex.

## Controles implementados

- `AccountMutationLease` acepta únicamente `alpaca/paper`, usa una identidad de
  scope SHA-256 y `flock(LOCK_EX|LOCK_NB)` sin TTL ni borrado por mtime. Un lease
  preexistente vacío, parcial o inválido bloquea el arranque: nunca se interpreta
  como stale ni se borra automáticamente. La creación sincroniza archivo y
  directorio; su generación local ya no es el fence publicado por el executor.
- El release no puede cerrar el descriptor mientras una mutación está activa.
- La raíz por defecto deriva de `pwd`, no de `HOME`/XDG controlados por el
  proceso, y exige directorio real, propiedad del usuario y modo privado.
- `build_alpaca_paper_client()` crea ahora un
  `SupervisedAlpacaPaperClient`; el único import/construcción paper del SDK está
  en `paper_account_executor.py`.
- El handshake exige `get_account().id` como UUID y status `ACTIVE`. `None`,
  booleanos, IDs no UUID y estados no activos bloquean antes de adquirir
  autoridad.
- No se acepta `account_id`, root, journal u output desde la API pública del
  constructor supervisado.
- El journal se deriva únicamente de
  `{canonical_root}/{scope_sha256}.orders.sqlite3`, se inicializa antes de
  operar, exige owner actual, link count 1 y modo `0600`, y conserva identidad
  de dispositivo/inode/metadata.
- Antes de cada POST/DELETE se revalidan lease, journal, cuenta y estado. Un
  cambio de cuenta o reemplazo del journal produce cero llamadas mutables al
  SDK.
- La dependencia broker está fijada exactamente a `alpaca-py==0.43.4`. El
  `TradingClient` real debe ser exactamente la clase SDK auditada y conserva
  `BaseURL.TRADING_PAPER=https://paper-api.alpaca.markets`, `sandbox=true`, API
  `v2`, autenticación key/secret no-basic, `raw_data=false` y OAuth ausente. Un
  cliente construido para live o cualquier cambio posterior de endpoint, modo
  o autenticación falla antes de la primera lectura/request físico. Cualquier
  otro `RESTClient`, incluido un subclass de `TradingClient`, se rechaza; sólo
  los fakes que no heredan de REST conservan el seam de tests. El
  constructor supervisado deshabilita los retries internos del SDK para
  POST/DELETE (`_retry=0`, sin espera ni códigos reintentables) y revalida esa
  configuración antes de leer la identidad de cuenta y justo antes de cada
  mutación. Además instala un timeout HTTP fijo de `5.0 s` antes de la primera
  lectura y atesta por identidad tanto el adaptador como el callable de
  transporte. Debajo del SDK descarta la sesión que traiga incluso un cliente
  real inyectado y crea una `requests.Session` exacta propiedad de la frontera,
  con `trust_env=false`, sin auth, proxies, cookies ni hooks heredados. Sus
  adapters HTTP/HTTPS usan cero retries y el guard conserva identidad de sesión,
  routing, adaptadores y objetos `Retry`. También fija los métodos de clase y
  prohíbe overrides por instancia en la ruta crítica `request → prepare →
  merge_environment_settings → send → get_adapter`, incluidos redirects y los
  métodos de dispatch del adapter. El wrapper revalida todo inmediatamente antes
  de cada request físico e impone tanto `timeout=5.0` como
  `allow_redirects=false`, aunque el caller intente sobrescribirlos; una
  alteración posterior bloquea antes de HTTP. Respuestas 301/307/308 conservan
  exactamente un `adapter.send`, sin follow-up. El mismo contrato se instala y
  revalida en los clientes Stock y
  Crypto market-data que construyen el daemon y la ingesta diaria gobernada;
  la lectura de precio ya no añade otra capa de retry. Los seams que aceptan un
  cliente/clase inyectado aplican el mismo guard si reciben un `RESTClient` real.
  Pruebas con el SDK real demuestran el timeout exacto y un
  único request físico ante 429, 504, `ConnectTimeout` y `ReadTimeout`, además de
  un único DELETE. Una versión no auditada o una alteración posterior del retry,
  sesión o guard bloquean antes de mutar. Pasar explícitamente la clase SDK real
  por el seam de tests tampoco evita el guard; los fakes no necesitan emular
  `requests.Session`.
- Para `submit`, el broker vuelve a comprobar kill switch y reducción, y obtiene
  y valida el precio dentro del lease; solo entonces hace el CAS durable
  `INTENT_RECORDED -> SUBMIT_ATTEMPTED`, inmediatamente antes del POST.
- Referencia, precio observado y umbral de desviación deben ser finitos y
  válidos. `NaN`, `+Inf`, `-Inf` y booleanos bloquean el submit antes del SDK.
- Un lookup broker-first indisponible o un fallo que la frontera supervisada
  demuestra anterior a `before_dispatch` se registra como
  `submit_not_dispatched`/`cancel_not_dispatched`, conserva el estado reintentable
  y devuelve `*_deferred`. Si no puede persistirse esa prueba, falla como
  unresolved. Si la excepción ocurre durante/después del CAS o después de
  `cancel_dispatch_attempted`, permanece ambigua y no autoriza otro POST/DELETE.
- La aplicación del executor conserva el `PaperOrderResult` en memoria hasta
  clasificarlo. La frontera supervisada registra una instancia exacta ligada a
  operación sólo si falla antes de entrar al cuerpo mutable; el broker consume
  esa prueba una vez y registra del mismo modo el resultado exacto. Ambos
  registros son débiles, thread-safe, no serializables y one-shot. El servicio
  exige `type(result) is PaperOrderResult` e invoca el método base, por lo que
  una excepción pública creada por un cliente raw, un atributo escrito con
  `object.__setattr__` o un override de subclase no autorizan retry y pasan a
  `OUTCOME_UNKNOWN`. Sólo esa prueba one-shot, ligada a operación dentro del
  daemon auditado, puede autorizar la transición `DISPATCHING -> RECORDED`; un
  string `*_deferred` sin prueba tampoco basta. El journal limita
  `dispatch_proven_not_started` a una vez por request/run, reclama el mismo
  comando y permite un único retry inmediato. Si vuelve a probarse no-despacho,
  termina como `predispatch_retry_exhausted`; si el CAS interno ya ocurrió o el
  resultado es ambiguo, no hay retry. Las legs de `safe-flatten` no adoptan este
  requeue: ampliar su autómata requeriría otra transición atómica coordinada con
  la leg y el workflow, por lo que hoy fallan cerradas como leg rechazada y el
  kill switch permanece latched.
- Este registro es una defensa contra resultados y excepciones forjados por los
  seams previstos, no una frontera frente a compromiso arbitrario del mismo
  proceso (por ejemplo monkeypatch de privados o una mutación concurrente entre
  validación y uso). El daemon de producción permanece aislado, no carga plugins
  ni admite inyección de clientes/brokers; preservar esa propiedad forma parte
  del gate de release.
- `AlpacaPaperBroker` sustituye cualquier journal caller-selectable por el
  journal canónico cuando recibe el cliente supervisado.
- Una prueba AST impide que aparezcan nuevos constructores `TradingClient` o
  nuevos `paper=True/False` fuera de las fronteras paper/live declaradas.
- Una segunda prueba AST prohíbe importar la conexión Alpaca paper directa
  fuera de su definición y del daemon. El inventario productivo de callers que
  construyen `build_alpaca_paper_client()` es ahora cero.
- La frontera live conserva solo una instantánea del reloj; ya no retiene un
  bound method cuyo `__self__` permitía recuperar el SDK crudo.

### Executor persistente e IPC

- `build_exclusive_alpaca_paper_client()` adquiere una sola autoridad durante
  toda la vida del proceso y deriva paths canónicos para el journal de órdenes
  y el ledger de comandos bajo un `supervisor_root` explícito.
- `DurableExecutorCommandJournal` mantiene lifecycle de runs, fence, policy
  hash, comandos y resultados terminales append-only. Un request ID no puede
  cambiar operación o payload; un estado `OUTCOME_UNKNOWN` bloquea nuevas
  mutaciones hasta obtener evidencia positiva.
- El fence publicado se asigna dentro de SQLite al crear el run: `BEGIN IMMEDIATE`,
  lectura de `MAX(fence_epoch)`, incremento e inserción con unicidad
  en la misma transacción. El caller no puede elegirlo y
  `PaperExecutorApplication` adopta el epoch devuelto por el journal, no la
  generación del lease. Un fallo antes del commit no consume epoch; después del
  commit el nuevo run queda durable y el siguiente recibe uno mayor.
- Los request IDs de submit, cancel y latch son deterministas y separados por
  dominio. Cancelar por `order_id=X` no colisiona con cancelar por
  `client_order_id=X`.
- El submit registra intención antes del POST. El cancel registra intento antes
  del DELETE y aceptación después de la respuesta. Un timeout o respuesta
  truncada posterior al envío nunca provoca retry automático.
- El transporte real auditado de `alpaca-py==0.43.4` fuerza un único intento y
  `timeout=5.0` en Trading, Stock y Crypto. Si la autoridad supervisada prueba
  que el fallo ocurrió antes de entrar al método SDK mutable, el executor
  registra `DISPATCHING → RECORDED` y permite exactamente un segundo intento
  del mismo request en el mismo run. El journal impide ampliar ese presupuesto;
  un segundo fallo pre-dispatch termina en `predispatch_retry_exhausted`. Un
  estado `submit_deferred`/`cancel_deferred` sin esa prueba one-shot ligada a la
  operación dentro del daemon auditado se trata como `OUTCOME_UNKNOWN`, nunca
  como permiso de retry. No constituye una frontera contra código arbitrario ya
  ejecutándose en ese mismo proceso; por eso el daemon no carga plugins ni seams
  de inyección en producción.
- En reinicio, `RECORDED` se cierra localmente como no despachado. Un submit
  ambiguo sólo se resuelve si `client_order_id` y la intención observada en el
  broker coinciden. Un cancel ambiguo sólo se resuelve con `pending_cancel` o
  estado terminal; ausencia, 404, timeout o mismatch permanecen bloqueados.
- El kill switch y el resultado terminal del comando se persisten de forma
  atómica. El ledger impide reclamar submits de apertura mientras está activo.
- `PaperExecutorClient` valida identidad de socket, response/request binding,
  schema exacto, target y DTOs de dominio. Tras un posible envío mutable,
  timeout, frame parcial o respuesta inválida se clasifican como outcome
  desconocido, no como fallo reintentable.
- `PaperExecutorBrokerClient` expone recibos tipados para submit y cancel. Un
  éxito o rechazo conserva request ID, operación, resultado, account scope,
  run ID, fence epoch y hashes de policy/authz; el método legacy devuelve el
  mismo resultado sin duplicar la mutación. Si el transporte pierde la
  respuesta después del envío, la excepción conserva el mismo target.
- Los callers mutantes fijan el target observado en preflight. Antes de cada
  mutación el cliente relee health y exige igualdad exacta de account scope,
  policy/authz hashes, run ID y fence epoch; un restart o cambio de policy
  bloquea antes de enviar el request mutable.
- El deadline de wire se convierte una sola vez al recibirlo en un presupuesto
  monotónico local. Las decisiones posteriores no vuelven a depender de saltos
  del reloj de pared.
- Cualquier path AF_UNIX preexistente bloquea el arranque. No hay probe de
  staleness ni `unlink` de inicio. El servidor sólo elimina el socket que él
  mismo enlazó, tras comprobar el dispositivo/inode exacto, al fallar su setup o
  cerrar normalmente; systemd es responsable del runtime entre instancias.
- La ruta fija `/run/trading-ai-paper/alpaca-paper-executor.sock` exige runtime
  real `0750` y socket `0660`, ambos propiedad del UID
  `trading-ai-paper-executor` y GID `trading-ai-paper-ipc`. El cliente verifica
  owner, grupo, modo, estabilidad del inode y UID del servidor con
  `SO_PEERCRED` antes de enviar.
- La policy IPC liga nombres y UIDs NSS estáticos distintos del daemon, exige
  membresía del grupo y separa `health`, `observe`, `reduce`, `cancel`, `kill` y
  `open`. La plantilla concede sólo observación al monitor y reducción/cancel/
  kill al safety worker. El daemon carga con `allow_open=False`, por lo que
  ninguna policy instalada puede habilitar apertura sin una nueva frontera de
  riesgo server-side revisada.
- La lectura pre-frame no bloquea el dispatcher: se admite como máximo un
  request pendiente por UID y 16 globales, la reserva continúa hasta enviar o
  cerrar la respuesta, y `close()` hace shutdown y join de todos los readers.
  Requests mutables listos preceden a observaciones listas y el deadline se
  revalida inmediatamente antes del handler. Esto evita que un observer lento
  monopolice por sí solo `cancel`/`kill`, sin hacer concurrente al escritor del
  broker.
- `PaperExecutorApplication` comprueba que broker, autoridad, cuenta, journal y
  fence pertenecen exactamente a la misma capability antes de declararse
  listo. Sus estados son `STARTING/RECOVERING/READY/BLOCKED/DRAINING/STOPPED`.
- `paper_executor_daemon.py` lee las credenciales Alpaca únicamente desde
  `CREDENTIALS_DIRECTORY`; no carga Fish, zsh, `.env`, shell, MCP ni ningún LLM.
  Cada policy file se abre y lee una sola vez: el hash y el parser consumen
  exactamente los mismos bytes. Exige que el archivo y toda su cadena de
  directorios sean root-owned e inmutables para el UID del servicio. La CLI ya
  no acepta `--supervisor-root`: deriva obligatoriamente la raíz canónica desde
  `STATE_DIRECTORY`. Publica capacidad `full`, `reduce_only` o `blocked` en
  health.
- `deploy/systemd/` contiene la unit endurecida, `sysusers.d`, plantilla authz y
  guía de provisión. Es un paquete versionado, no un instalador: no crea UIDs,
  no instala archivos, no habilita ni arranca servicios. La unit usa
  `LoadCredentialEncrypted=`, `DynamicUser=no`, `NoNewPrivileges=yes`, runtime
  `0750`, state `0700`, sin socket unit, timer, `EnvironmentFile` ni capacidad
  `open`.
- `paper_monitor`, `scripts/export-alpaca-paper-statement.py`, las rutas CLI de
  Gate 1, sleeve position watch, sleeve revalidate y sleeve circuit breaker, y
  `paper_close_session` ya usan `PaperExecutorBrokerClient` sin recibir
  credenciales ni SDK. Las acciones del breaker conservan las tres
  confirmaciones y sólo llegan al executor como `reduce`/`close` desde el UID
  safety autorizado.
- `paper_execute_session` ya usa la misma fachada IPC y fija el target. En esta
  fase sólo permite una reducción pura; una rotación close→open se bloquea
  antes de la primera mutación, toda apertura se difiere, los receipts quedan
  ligados al fence y un outcome desconocido es terminal/no-retry para el
  caller.
- `sleeve-rebalance --real-paper` tampoco construye SDK, market data, broker ni
  journal local. Valida y fija health, ejecuta únicamente `sell/reduce/close` a
  mercado antes de representar compras como diferidas, y deja cualquier
  reducción aceptada en `BLOCKED` hasta reconciliación terminal. Limit-maker se
  difiere completo porque submit→cancel→fallback aún no es un workflow durable
  del executor. Fallos de health o posiciones reemplazan evidencia stale por
  un artefacto `BLOCKED` sin incluir el mensaje potencialmente sensible.
- El subcomando CLI `paper` ya está migrado: dry-run usa únicamente el broker
  sintético sin IPC; cada acción real usa `PaperExecutorBrokerClient`, sin SDK,
  credenciales, market data ni fallback en el caller. Las acciones son
  mutuamente excluyentes salvo la lectura conjunta de cuenta/posiciones, los
  estados de lista se validan en argparse y las opciones huérfanas fallan antes
  de crear clientes. Las aperturas quedan bloqueadas cuando health no publica
  `opening_orders_allowed=true`; kill switch activo y estados no listos tienen
  motivos separados.
- Submit y cancel desde esa CLI escriben un recibo de executor correlacionado.
  Un resultado desconocido produce exit 2, reporte y ledger
  `OUTCOME_UNKNOWN`, `retry_allowed=false` y una instrucción explícita de
  reconciliar antes de cualquier reintento. No existe retry ni fallback.
- `scripts/alpaca-mcp-paper.sh` está cerrado: no lee Fish, zsh ni `.env`, emite
  un diagnóstico y termina con código 64. Un futuro adaptador MCP deberá ser
  GET-only y cruzar la misma frontera IPC sin credenciales.
- MiniMax-M3 está excluido de toda esta superficie: no recibió ni puede editar
  código de execution/risk, credenciales, estado broker o controles de
  promoción.

### Safe-flatten durable, sin activación

- El ledger v4 agrega operación, eventos, legs y eventos de legs en el mismo
  SQLite de autoridad. La migración v3→v4 es aditiva y valida el esquema v3
  exacto antes de crear objetos nuevos.
- El inicio, el latch y el resultado del comando `start_safe_flatten` se
  confirman en un único `BEGIN IMMEDIATE`. No existe una ventana durable con
  operación creada y kill switch ausente.
- Un crash de una leg `dispatching` mueve comando, leg y operación a ambigüedad
  en la transacción que adopta el nuevo run. La recuperación positiva también
  actualiza los tres historiales en una sola transacción.
- El workflow bloquea todas las mutaciones externas mientras esté activo. El
  health lógico publica `flattening`, aunque el run durable siga `READY` para
  que sólo el daemon pueda ejecutar sus legs internas.
- La reconciliación del journal de órdenes rechaza cualquier estado no
  terminal; los terminales se mueven juntos a `reconciled` y producen una
  atestación con cantidad de registros, máxima secuencia y SHA-256 de la
  proyección materializada.
- El daemon hace un paso acotado por iteración y como máximo un POST/DELETE. El
  caller no dispone de RPC de avance, no recibe posiciones/cantidades y nunca
  puede resetear el latch.
- Esta implementación no fue instalada ni activada y no ejecutó llamadas a
  Alpaca. Ya pasó una prueba cross-UID real en user namespace y una primera
  inyección `SIGKILL`, pero sigue siendo NO-GO hasta validar el paquete y los
  callers bajo systemd/NSS reales, ampliar fault injection a todas las ventanas
  broker y completar una campaña paper representativa.

## Evidencia ejecutada

Evidencia focalizada repetida sobre el árbol actual el 2026-07-15:

- journal del executor, aplicación, cliente de alto nivel, authz, daemon y
  empaquetado: 97/97;
- transporte AF_UNIX: 29/29 fuera del sandbox, porque el sandbox deniega
  `bind(AF_UNIX)` con `EPERM`;
- lease y frontera de cuenta paper: 34/34;
- migración de la CLI `paper`, fachada, conexión y observabilidad: 96/96;
- migración read-only de `paper-position-watch`: 13/13;
- workflow durable de `paper-safe-flatten`, journal global de órdenes y
  adaptadores relacionados: 186/186;
- endurecimiento final de endpoint/sesión/redirect/predispatch: suite focal
  93/93 y conjunto Alpaca/executor relacionado 249/249;
- última regresión canónica completa fuera del seccomp, antes de los cambios
  aislados del bridge MiniMax: 1.916 pruebas correctas, 3 omitidas y cero fallos
  en 137.664 s;
- verificación actual dentro del seccomp administrado: 1.894 correctas, 34
  omitidas y 34 errores de entorno, todos en `bind(AF_UNIX)` del módulo IPC; la
  repetición canónica fue bloqueada por el límite de uso de la plataforma;
- `systemd-analyze verify` de la unit empaquetada: verde; esto valida sintaxis,
  no instalación ni operación con UIDs reales;
- `systemd-analyze security --offline=yes`: exposición 1.5 (`OK`), y
  `systemd-sysusers --dry-run`: verde sin crear usuarios/grupos;
- probe efímero sin broker ni credenciales con UIDs kernel distintos dentro de
  un user namespace: monitor UID 2 observa, no puede mutar; safety UID 3 cruza
  el gate `kill` no-op; un UID desconocido del grupo es rechazado y un UID fuera
  del grupo recibe `EACCES`. El test no parchea `SO_PEERCRED`; pasó 1/1 fuera
  del seccomp y se omite antes de `READY` cuando `newuidmap` no conserva caps;
- inyección física `SIGKILL` después de `dispatch_claimed` y antes del commit:
  SQLite reabre en `RECORDED`, sin evento parcial, `full_audit()` verde y el
  sucesor resuelve `command_not_dispatched` sin broker;
- reinicio de un cierre safe-flatten con `submit_unresolved`: una observación
  positiva por el mismo `client_order_id` reanuda `CLOSING` y mantiene una sola
  llamada submit en el fake broker;
- las rutas habituales de unidades del host no contienen
  `trading-ai-paper-executor.service`; el bus systemd no fue accesible desde el
  entorno de auditoría, por lo que no se afirma un estado runtime adicional.

El inventario reproducible con
`rg 'build_alpaca_paper_client\(' src scripts` encuentra únicamente la
definición: **cero invocaciones productivas directas**. El AST gate permite
importar `trading_ai.execution.alpaca_connection` sólo desde el daemon. Este
recuento es una propiedad de la release en el repositorio; no demuestra todavía
que procesos o timers desplegados usen esa release.

La regresión completa se repitió después de los últimos cambios de authz,
transporte y migración de consumidores. Ese resultado elimina la deuda de
regresión conocida del árbol actual, pero no sustituye pruebas multi-UID/systemd,
fault injection, una campaña paper representativa ni autoriza el cutover.

## Estado de las dos últimas migraciones de consumidores

La auditoría de los callsites restantes concluyó que no es seguro sustituir el
constructor sin separar responsabilidades:

- La CLI `paper` completó esa etapa: conserva el broker sintético para dry-run,
  usa IPC para health/observe/cancel/submit real, devuelve
  `opening_orders_disabled` mientras el executor está en `reduce_only` y no
  contiene fallback directo. Falta todavía fijar todas las observaciones
  multi-RPC al mismo target o snapshot y clasificar `order_missing` en la
  reconciliación sin convertir errores indeterminados en ausencia de orden.
- `sleeve-rebalance` completó sólo la primera fase segura: plan offline,
  snapshots observados y pase reduce/close bajo IPC. Las aperturas se eliminan
  del pase y quedan diferidas; una compra denegada ya no puede impedir una
  reducción. Aún faltan intent de campaña durable, reconciliación terminal,
  recálculo server-side y un ciclo posterior bajo UID allocator. `cancel` de
  ese futuro allocator deberá quedar limitado a su namespace o convertirse en
  una operación semántica server-side.
- `paper_position_watch` ya migró todas sus lecturas al facade IPC y volvió a
  ser estrictamente read-only. Su flag histórico de cierres falla antes de
  construir un cliente y deja un artefacto ERROR: no se expone
  `mark_order_reconciled` ciego. Para rehabilitarlo, el executor debe verificar
  intent canónico, orden filled, cantidad completa, identidad broker, posición
  residual cero y ausencia de orden activa antes de cerrar el journal.
- `paper_safe_flatten` completó la migración a workflow durable del executor:
  `latched → canceling → cancel_confirmed → closing → fills_confirmed →
  reconciling → flat_latched`. El caller persiste `operation_id` antes del RPC,
  inicia como máximo una vez y luego sólo consulta estado. El daemon ejecuta
  legs seriales; `pending_cancel` no es terminal, un close sólo verifica con
  identidad y cantidad filled exactas, y la reconciliación global del journal
  es atómica y atestada. Un resultado ambiguo bloquea y se recupera
  broker-first en una sola transacción comando+leg+operación, sin segundo
  POST/DELETE. `FLAT_LATCHED` conserva el kill switch y el reset sigue fuera
  del RPC online.
- `paper_execute_session` observa y reduce por IPC con target fijado, pero las aperturas
  quedan bloqueadas hasta que el daemon tenga estado de riesgo durable
  account-scoped, snapshot atómico o atestado, policy hash ligado al paquete y
  una decisión tipada `allowed/reasons/context`. Ninguna métrica de riesgo
  enviada por el caller puede autorizar exposición.

Cada reporte mutante deberá conservar un recibo correlacionable con el journal:
request ID, account scope, run ID, fence epoch y hashes de policy/authz. Las
pruebas de aceptación incluyen replay exacto, colisión de payload, target stale,
namespace de cancel, partial fill, exposición residual, restart y ausencia total
de credenciales/SDK/fallback en los consumidores.

## Límites que impiden aceptar P0-05

1. No se hizo cutover. El inventario del repositorio ya tiene cero invocaciones
   directas, pero los timers/procesos activos no se migraron ni se probaron
   contra la frontera IPC instalada. La autoridad única aún no es una propiedad
   demostrada del sistema en ejecución. El paquete contiene la unit del daemon,
   pero todavía no units endurecidas para ejecutar monitor y safety bajo sus
   identidades dedicadas; no se añadirá el usuario interactivo a authz como
   atajo.
2. La authz cross-UID pasó sin mocks dentro de un user namespace y el paquete
   systemd supera verify/security offline, pero los usuarios, grupo, policy
   root-owned, credenciales cifradas y unit no están instalados ni operados.
   Falta repetirlo con NSS/systemd reales y una prueba SIGKILL→restart que
   confirme el cleanup de `RuntimeDirectoryPreserve=no`. También falta medir en
   ese host latencia de `cancel`/`kill` bajo flood: un
   frame lento ya no bloquea, pero un handler broker que ya empezó no es
   preemptible y un backlog de conexiones a nivel kernel requiere validación de
   carga o sockets de seguridad separados si se necesita una cota más fuerte.
3. La capacidad de apertura permanece cerrada porque todavía no existe un
   proveedor de riesgo server-side durable que derive exposición, PnL,
   drawdown y presupuesto desde snapshots atestados. Confiar esos números al
   caller volvería a abrir la frontera de confianza.
4. `flock`, SQLite y fence siguen siendo coordinación de un solo host. Alpaca
   no valida el epoch y no existe exactly-once atómico entre journal local y
   broker; por ello la recuperación es broker-first y conservadora, no una
   promesa de exactly-once distribuido.
5. El launcher MCP heredado ya está deshabilitado y no carga credenciales, pero
   aún debe demostrarse por inventario de procesos y `/proc`/entorno que ninguna
   otra ruta conserva credenciales capaces de saltarse el executor.
6. La metadata del lease se actualiza mediante `ftruncate` sobre el mismo inode.
   Un crash puede dejarla vacía o parcial y bloquear el siguiente arranque. Ese
   estado ya falla cerrado y no puede retroceder el epoch publicado en SQLite,
   pero sigue siendo una debilidad de disponibilidad y diagnóstico que conviene
   separar en un lock estable más metadata atómica.
7. Faltan pruebas de proceso real con SIGKILL en cada frontera
   record→claim→POST/DELETE→response→complete, corrupción/disco lleno, restart
   systemd y particiones broker.
8. La última regresión completa ejecutó 1.962 tests: 34 errores se concentraron
   exclusivamente en el `bind(AF_UNIX)` del módulo IPC porque el seccomp
   administrado no permite crear esos sockets, y 34 casos fueron omitidos. La
   repetición fuera de ese seccomp fue bloqueada por el límite de uso de la
   plataforma. Las suites focales IPC/financieras habían pasado fuera de esa
   restricción, pero la release actual no se declarará completamente verde
   hasta repetir el árbol en el entorno canónico. Además falta una campaña
   paper representativa con duración y criterios predefinidos.
9. La evidencia de costes, fills/fees, reconciliación y rentabilidad ajustada a
   slippage sigue incompleta. Una arquitectura de ejecución más segura no hace
   rentable una estrategia ni autoriza dinero real.
10. El timeout de `requests` limita conexión y espera entre lecturas, no es un
    deadline total preemptivo: resolución DNS o una respuesta que entregue bytes
    lentamente puede superar `5.0 s`. El handler broker que ya empezó tampoco se
    cancela por la expiración lógica del comando. Se mantiene fail-closed, pero
    la cota dura requiere aislamiento/watchdog de proceso y validación de carga.
    Este cambio se limita al executor paper; no amplía ni autoriza la frontera
    live.

## Cutover obligatorio, todavía no autorizado

- Fijar una release e instalar los artefactos existentes de `deploy/systemd/`
  fuera del checkout: provisionar UIDs/grupo estables, renderizar la policy
  root-owned y cargar credenciales con `LoadCredentialEncrypted=`.
- Validar la autorización IPC con procesos reales bajo UIDs distintos y dejar
  `open` ausente; el grupo sólo debe conceder acceso al socket, nunca autoridad
  semántica.
- Conservar el gate que mantiene en cero el recuento directo; migrar los timers
  a los consumidores IPC de esta release y verificar que escriben evidencia
  `BLOCKED` si el daemon no está listo.
- Mantener el launcher MCP deshabilitado y eliminar credenciales de cualquier
  proceso de estrategia o auxiliar; verificar por `/proc`/entorno que sólo el
  daemon las posee.
- Ejecutar fault injection y campaña paper en paralelo con comparación de
  snapshots, sin doble escritura; después realizar un cutover reversible
  explícitamente autorizado.
- Mantener apertura bloqueada hasta que riesgo, breaker, presupuesto diario,
  policy hash y evidencia financiera sean computados por el executor desde
  fuentes durables y pasen gates independientes.

Hasta completar esos puntos, `live_trading_allowed=false` permanece obligatorio
y ninguna métrica paper constituye evidencia suficiente de rentabilidad real.
