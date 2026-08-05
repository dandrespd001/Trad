# Integración autónoma Codex → MiniMax-M3 por API

Fecha de auditoría: 2026-07-15. Contrato del runner: v0.3.5; verificador: v0.3.0.

## Veredicto

La ruta adoptada no requiere hardware local. Codex puede automatizar cada paso
del workflow sin una aprobación humana separada por job cuando ya existe
autorización de egress y la política activa de la plataforma permite la red:

```text
Codex principal (arquitecto/director)
  -> spec repo-relative + allowlist mínima
  -> snapshot exacto y DLP local best-effort
  -> MiniMax-M3 Token Plan Responses API, sin tools y con un solo POST
  -> patch congelado + SHA-256 + export determinista
  -> revisión Codex + verificador Bubblewrap sobre proyección privada
  -> aceptar, reparar una vez o rechazar
```

MiniMax nunca controla el checkout, Git remoto, shell, MCP, plugins, navegador,
broker ni trading live. Codex conserva la decisión final. `--cloud-approved`
registra una autorización existente, pero no evita una aprobación o denegación
de red/data-egress impuesta por la plataforma. Los gates deterministas siguen
por encima de ambos modelos.

## Decisión frente a las alternativas

| Ruta | Codex dirige | Paso humano interno por job | Superficie | Decisión |
|---|---:|---:|---|---|
| MiniMax como modelo principal de la sesión | No | No | Toda la sesión | Rechazada |
| Claude Code + `/anthropic` | Parcial | No | Otro agente y permisos | Rechazada |
| MCP/plugin que envuelve MiniMax | Sí | No | Canales laterales adicionales | Rechazada |
| Segundo `codex exec` con MiniMax | Sí | No | Features/plugins/browser del proceso | Rechazada tras auditoría |
| Cliente directo `/v1/responses`, sin tools | Sí | No, si política permite egress | Un request y un patch | Adoptada |

El segundo `codex exec` se descartó en la auditoría con Codex CLI 0.144.1 porque
conservaba múltiples features activas por defecto y `codex doctor` intentó
tráfico auxiliar incluso con updates deshabilitados. El inventario del
2026-07-15 usa Codex CLI 0.144.4 y no cambia la decisión: un cliente directo
permite limitar el único egress al request MiniMax y demostrar que el modelo no
dispone de herramientas.

### Encaje con las superficies oficiales de Codex

El manual vigente de Codex recomienda empezar con una Skill local para un
workflow personal o de un solo repositorio, y reservar un plugin para distribuir
Skills, conectores, MCP o hooks como paquete estable. También confirma que
`AGENTS.md` se carga automáticamente al inicio de cada sesión y que una Skill
puede activarse implícitamente cuando la tarea coincide con su descripción. Por
eso la combinación adoptada es `AGENTS.md` + `$delegate-minimax-api`: un plugin
o MCP no mejora la autonomía de este caso y sí ampliaría la superficie.

Fuentes oficiales: <https://learn.chatgpt.com/docs/build-skills>,
<https://learn.chatgpt.com/docs/agent-configuration/agents-md> y
<https://learn.chatgpt.com/docs/build-plugins>.

Codex controla internet por entorno y permite dominio/método sólo cuando la
política del host lo autoriza. Esa frontera explica por qué la autorización
permanente elimina el paso humano interno del runner, pero no puede convertir
una denegación administrada de red en permiso:
<https://learn.chatgpt.com/docs/cloud/internet-access>.

## Contrato oficial verificado

MiniMax documenta para Codex:

- modelo exacto `MiniMax-M3`;
- contexto de 1.000.000 tokens;
- endpoint global `https://api.minimax.io/v1`;
- `wire_api = "responses"` y Bearer auth.

Fuente: <https://platform.minimax.io/docs/token-plan/codex>.

La Responses API oficial expone `POST /v1/responses`, razonamiento, un
`prompt_cache_key`, métricas de tokens y salida de texto:
<https://platform.minimax.io/docs/api-reference/responses-create>.

El esquema actual documenta texto, no JSON Schema. Por eso no se depende de
`codex exec --output-schema`: el corredor exige localmente `diff --git` o
`NO_CHANGE` y rechaza cualquier otra forma.

## Credencial Fish y proceso aislado

La credencial correcta permanece en:

```text
~/.config/fish/config.fish
function claude-minimax
  set -lx ANTHROPIC_AUTH_TOKEN <redacted>
end
```

El corredor extrae exactamente un literal desde esa función, exige el prefijo
Token Plan `sk-cp-` y lo usa en memoria como Bearer del endpoint Responses. No lo
copia a TOML, entorno de subprocesses, manifest, logs, patch ni salida, y no
hace sustitución por una clave estándar pay-as-you-go. El archivo Fish debe pertenecer al usuario y
tener modo `0600`.

La clave de suscripción no equivale a un coste fijo garantizado: MiniMax indica
que usa primero la cuota incluida y, si existen Credits comprados, puede
consumirlos automáticamente al agotar cuota elegible. Responses no documenta un
switch por request para impedirlo. Cada job declara este contrato; para exigir
cero consumo incremental de Credits se necesita que la cuenta no los tenga o
un guard externo de cuota/cuenta.

No se usa Zsh ni el wrapper histórico
`~/.config/trading-ai/minimax-exec.sh`. El runner se invoca con
`/usr/bin/python3 -I` para aislarlo de paquetes de usuario y personalizaciones
de inicio de Python.

## Implementación

- Fuente versionable: `skills/delegate-minimax-api/`.
- Runner v0.3.5: `skills/delegate-minimax-api/scripts/minimax_api_worker.py`.
- Verificador Codex-only v0.3.0:
  `skills/delegate-minimax-api/scripts/minimax_patch_verify.py`; no conoce
  endpoint, Fish ni token y no ofrece ninguna herramienta a MiniMax.
- Skill personal primaria: `~/.agents/skills/delegate-minimax-api`, que es la
  ubicación global vigente de Codex.
- Respaldo heredado deshabilitado fuera de la ruta escaneada:
  `~/.codex/delegate-minimax-api.disabled-20260714`. Mantener una sola Skill
  activa evita dos entradas con el mismo nombre; Codex no las fusiona.
- Comando opcional: `~/.local/bin/minimax-api-worker`, instalado como copia
  regular privada del runner auditado, nunca como symlink. Así el hash propio y
  la procedencia no pueden cambiar por retargeting del enlace.
- Tests: `tests/test_minimax_api_worker_cli.py` y
  `tests/test_minimax_patch_verify_cli.py`.

Después de instalar o actualizar la skill global, se debe abrir una sesión
nueva de Codex para que el catálogo de skills la descubra de forma fiable.

El runner no ofrece `apply`, commit, push, deploy ni operaciones broker. Solo
crea, ejecuta, consulta, exporta y purga jobs aislados.

El inventario efectivo de Codex confirma una sola Skill MiniMax activa y cero
MCP o plugins MiniMax. El MCP heredado de Alpaca permanece deshabilitado. Esta
separación es intencional: envolver el mismo request en MCP/plugin no elimina
aprobaciones administradas y sí agrega otro canal de herramientas. Los MCP
globales genéricos que usan paquetes `@latest` deben perfilarse o fijarse por
separado; no forman parte ni son requisito del bridge MiniMax.

## Operación automática

Diagnóstico local, sin red:

```bash
/usr/bin/python3 -I skills/delegate-minimax-api/scripts/minimax_api_worker.py \
  --json --repo . doctor
```

El probe opcional llama únicamente a `GET /v1/models`, no envía código y puede
reintentar fallos transitorios:

```bash
/usr/bin/python3 -I skills/delegate-minimax-api/scripts/minimax_api_worker.py \
  --json --repo . doctor --probe
```

Crear un spec UTF-8 dentro del repositorio y ejecutar el job:

```bash
/usr/bin/python3 -I skills/delegate-minimax-api/scripts/minimax_api_worker.py \
  --json --repo . job create \
  --cloud-approved \
  --spec task-spec.md \
  --allow-path src/trading_ai/example.py \
  --allow-path tests/test_example.py

/usr/bin/python3 -I skills/delegate-minimax-api/scripts/minimax_api_worker.py \
  --json --repo . job list

/usr/bin/python3 -I skills/delegate-minimax-api/scripts/minimax_api_worker.py \
  --json --repo . job status JOB_ID

/usr/bin/python3 -I skills/delegate-minimax-api/scripts/minimax_api_worker.py \
  --json --repo . job run JOB_ID --reasoning none

/usr/bin/python3 -I skills/delegate-minimax-api/scripts/minimax_api_worker.py \
  --json --repo . job diff JOB_ID
```

El spec debe ser un archivo regular repo-relative. Se rechazan rutas absolutas,
`..`, symlinks y `.`. `job list` permite recuperar automáticamente los IDs sin
inspeccionar estado privado: es read-only, no crea estado ni clave HMAC si no
existen y sólo devuelve resúmenes públicos acotados. Antes de `job run`, Codex
usa `job status` para revisar el manifest sellado: spec y hash, selecciones
permitidas, archivos y hashes, omitidos, contrato de billing, hash del runner y
hash del prompt. Esta revisión la hace el supervisor automáticamente; no concede
ni sustituye una autorización administrada de egress. `job diff` no admite
`--out`: devuelve la ruta privada y determinista
`/tmp/minimax-api-worker-$UID/exports/JOB_ID.patch`.

Después Codex verifica los hashes y la procedencia, revisa el diff y rechaza
hooks de arranque, inicializadores, ejecutables o capacidades protegidas antes
de ejecutar código. Repite el hash revisado al verificador:

```bash
/usr/bin/python3 -I \
  skills/delegate-minimax-api/scripts/minimax_patch_verify.py \
  --json --repo . verify JOB_ID \
  --expect-patch-sha256 REVIEWED_SHA256 \
  --venv .venv312 \
  --focused-argv-json '["/runtime/venv/bin/python","-S","-P","-m","unittest","tests.test_example","-v"]' \
  --release-argv-json '["/runtime/venv/bin/python","-S","-P","-m","unittest","discover","-s","tests","-q"]'
```

El verificador no copia el dirty checkout. Materializa el HEAD sellado con un
solo `git cat-file --batch` sin lazy fetch, hooks, checkout, clone, worktree,
smudge ni filtros; superpone únicamente el snapshot seleccionado y aplica el
patch congelado sólo a esa proyección privada. `/work` se monta read-only en
Bubblewrap, con red aislada (sólo loopback), HOME/tmpfs sintéticos, entorno
allowlisted, runtime read-only, user namespaces anidados deshabilitados y cero
configuración Fish, sockets o credenciales del host. Cada argv es un array JSON
directo y release sólo corre si focused pasa. El parser rechaza shells, `env`,
scripts Python directos y launchers de consola como `pytest`. Con `--venv`,
todo Python focused/release usa exactamente `/runtime/venv/bin/python` con
`-S -P` antes de `-m`, `-c` o script; sin venv, el Python de sistema exige
`-I -S -P`. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` impide carga implícita de
plugins. El preflight interno conserva su Python fijo aislado.

No existe fallback sin sandbox: si la política/seccomp del host impide crear el
namespace de red, el resultado es `sandbox_unavailable`. Dentro del sandbox
administrado actual ese namespace requiere la aprobación exacta de ejecución;
fuera de ese seccomp el perfil se validó con Bubblewrap 0.11.2. Como `/work` es
read-only, una prueba que intente escribir dentro del repositorio falla; debe
usar `/tmp`, HOME sintético o configurarse explícitamente sin abrir un bind
escribible del checkout.

Linux contabiliza `RLIMIT_NPROC` para todo el UID del host, no sólo para el PID
namespace nuevo: un límite absoluto de 256 resultó insuficiente en una sesión
gráfica/Codex con unas 2661 tareas existentes y produjo `EAGAIN`. El verificador
lee ahora el `pids.current` agregado del slice cgroup-v2 exacto del UID, contador
que incluye hilos aunque el PID namespace oculte `/proc/*/task`, y pasa al hijo
`current + 256`, limitado por 4096 y el hard limit heredado. Si no puede medirlo,
si current ya alcanza el cap o si no puede aplicar el límite, falla antes de
ejecutar el candidato. `RLIMIT_AS` limita cada proceso a 4 GiB o al hard limit
menor; es defensa contra memoria por proceso, no una cuota cgroup agregada.

Antes de montar un venv, el verificador recorre de forma acotada hasta 100.000
entradas y 16 GiB de tamaños regulares de `site-packages`; rechaza symlinks,
otros mounts, sockets, FIFO, devices, `sitecustomize.py` y
`usercustomize.py`. El venv sigue siendo una dependencia confiable mutable del
host: puede cambiar después del recorrido y su árbol de más de 6 GiB no se
copia ni se pinnea íntegramente en esta revisión. Ese residuo requiere
ownership, inmutabilidad y ciclo de vida controlados en el host.

El resultado conserva fase, exit/timing, bytes y SHA-256 de salida, pero omite
el contenido capturado. Ese contenido nunca se reutiliza automáticamente como
repair delta; Codex construye y sanea explícitamente un delta mínimo, sin pedir
intervención al operador. La proyección se
purga en todos los resultados y cualquier fallo de cleanup retiene y reporta
la ruta privada como rechazo. Sólo tras `PASS` Codex puede aplicar el mismo
patch revisado al checkout real. Ante un fallo puede crear un único repair job
con delta mínimo saneado; no mantiene una conversación larga. Al terminar:

```bash
/usr/bin/python3 -I skills/delegate-minimax-api/scripts/minimax_api_worker.py \
  --json --repo . job purge JOB_ID
```

`--cloud-approved` no solicita ni concede una autorización por ejecución: deja
registro técnico de una autorización explícita ya dada al supervisor. Codex
puede añadirlo mientras esa autorización y la política de plataforma sigan
vigentes. La plataforma todavía puede exigir aprobación de red o bloquear el
envío; el runner no intenta saltarse ese control.

## Controles de egress y secretos

- Destino único exacto: `https://api.minimax.io/v1`, TLS 443.
- Modelo exacto `MiniMax-M3`, tier `standard` y clave Token Plan `sk-cp-`.
- Proxies y redirects deshabilitados.
- Sin override de modelo, host, tier o credencial. La Subscription Key consume
  primero la cuota incluida y puede pasar automáticamente a Credits comprados
  existentes; Responses no ofrece un switch por request para impedirlo. Cero
  gasto incremental exige un control externo de cuenta/cuota o no disponer de
  Credits comprados.
- Snapshot máximo de 80 archivos y 300 KB; input serializado máximo de 400 KB
  y output máximo de 50.000 tokens; archivo máximo de 512 KB.
- Solo texto UTF-8; se rechazan symlinks, traversal y selección de la raíz.
- Se deniegan `.env`, claves, credenciales, datasets, reportes, logs, notebooks,
  `.agents`, `.claude`, `.codex`, la skill de delegación y su contrato de
  auditoría.
- Se deniegan broker, execution, live, promotion, controles financieros/riesgo
  y deploy. En este repositorio también se deniegan por completo `configs/` y
  `scripts/`, el CLI/cargador de configuración central, la política de datos
  aprobados y los manifests de paquete/contenedor: son superficies transitivas
  de autoridad aunque tengan nombres genéricos.
- La v0.3.5 cierra además los nombres genéricos de autoridad que antes podían
  atravesar una allowlist estrecha: `setup.py/cfg`, `requirements*`,
  `package*.json`, Make/Task/Just, pre-commit, lockfiles, manifests de build y
  variantes Docker/Containerfile/Compose/deploy. Los nombres cercanos que son
  módulos ordinarios siguen permitidos para no convertir la política en un
  bloqueo por substring.
- DLP best-effort de nombres y contenido para claves privadas, tokens, secretos
  literales, arrays/tuplas, XML, Fish, Basic Auth, entropía alta y URLs con
  credenciales. El output se vuelve a escanear antes de persistirlo.
- Se rechazan flags live truthy incluso entre comillas, cambios de modo
  financiero protegido y archivos ejecutables generados.
- Se deniegan `__init__.py`, `__main__.py`, `sitecustomize.py`,
  `usercustomize.py`, plugins/entrypoints y nuevas referencias a execution/risk,
  broker/live, entorno/credenciales, red, subprocess o ejecución dinámica.
- Sólo se delegan módulos Python puros. Si el baseline sellado o el archivo
  generado contiene cualquier capacidad protegida, se rechaza el archivo
  modificado completo; no puede activarse un helper preexistente ni cambiar
  `if False` a `True`. AST y regex cubren aliases y, explícitamente,
  `pathlib`/`Path.home`/`joinpath`/`read_text`, `io`, `open` y
  `urllib3.PoolManager.request`, además de entorno, red, subprocess, dynamic
  loading, broker/live y execution/risk. Es una política conservadora para
  módulos puros, no análisis semántico o de taint completo.
- Git usa entorno mínimo, sin variables heredadas ni configuración global.
- MiniMax recibe cero tools y solo devuelve texto.

El DLP reduce errores accidentales, pero no garantiza que un texto arbitrario
carezca de información sensible. Codex debe revisar spec, allowlist, archivos
omitidos y manifest antes de autorizar el egress. Un firewall de host limitado a
`api.minimax.io:443` sigue siendo defensa en profundidad.

## Estado, procedencia e integridad

- Todo el estado queda bajo `/tmp/minimax-api-worker-$UID/state`; por defecto,
  cada repositorio usa `<repo-hash>/<job-id>` con permisos privados.
- La creación se publica mediante rename atómico de un sibling privado
  `.creating-*`; purge renombra primero a `.purging-*` y después elimina el
  tombstone. El inventario read-only sólo considera hijos job directos y limita
  512 entradas, 256 jobs, 16 MB de manifests y profundidad JSON 128; exceso o
  manifest inválido falla cerrado.
- Los exports quedan bajo `/tmp/minimax-api-worker-$UID/exports`.
- Un HMAC local hace tamper-evident el manifest, estado, allowlist, hashes y
  resultado.
- Cada job registra `source_basis=selected_working_tree_snapshot`,
  `source_head_commit` y `source_snapshot_sha256`.
- Cada fuente y spec debe carecer de bits ejecutables. El manifest sella
  `source_executable=false` por archivo dentro de `source_snapshot_sha256`; un
  `chmod +x` posterior invalida run, diff y verificación.
- El hash de snapshot describe exactamente los archivos seleccionados, incluso
  contenido no committeado cuando se eligió deliberadamente.
- HEAD, archivos seleccionados y spec se revalidan antes del request y del
  export; cualquier cambio invalida el job y exige crear otro.
- El output se aplica solo al snapshot para validar sintaxis y alcance.
- `job create` es transaccional: cualquier fallo anterior al manifest sellado
  elimina el directorio incompleto y no deja fuentes huérfanas sin `job.json`.
- Se rechazan binary patches, symlinks, modos, renames, paths externos, exceso
  de archivos o tamaños.
- `provider-output.txt`, `provider.patch` y `changes.patch` se crean con
  `openat`/no-follow/exclusive, identidad verificada y publicación atómica sin
  reemplazo. Un symlink o archivo preposicionado falla cerrado sin truncar el
  sentinel o checkout apuntado. `changes.patch` se congela y su SHA-256 queda
  sellado.
- `job diff` solo funciona en `PATCH_READY`, no acepta destino elegido por el
  caller y nunca reemplaza contenido diferente en el export determinista.
- Un lock impide dos ejecuciones concurrentes del mismo job.
- El verificador exige runner/contrato v0.3.5, hash del patch y el SHA-256 fijo
  auditado del worker adyacente
  `5fdd4dcf32e47ca5c27b169d7472ecd036cd7fcf7b90b0de6d15b4ea43b7498d`;
  no acepta cualquier binario autodeclarado. Ignora dirty/untracked no
  seleccionados y vuelve a comprobar HEAD, manifest y modo de fuentes antes de
  aceptar.
- La proyección queda en un hijo directo privado de
  `/tmp/minimax-api-worker-$UID/verify`, nunca incluye el `.git` real y se monta
  read-only. Un cleanup incompleto es `verification_cleanup_failed`, no PASS.

## Concurrencia y retries

Un lock global por usuario en `/tmp/minimax-api-worker-$UID/.api-call.lock`
permite un único slot de POST Responses de jobs a la vez. Si vence el timeout
acotado de cola, el comando informa el error pero el job permanece `CREATED` y
reintentable; no se inició ningún request al proveedor ni se consumieron tokens
por ese intento de cola.

Bajo ese lock, antes de marcar `RUNNING`, cargar la clave Fish, hacer `fork` o
permitir el POST, el runner publica con `O_EXCL|O_NOFOLLOW`, modo `0600` y
`fsync` un guard global `.provider-request-unconfirmed.json`. No contiene clave
ni fuentes: registra job/attempt, endpoint/model, fingerprint canónico y el
SHA-256 del método+URL+body exactos. El manifest terminal con
`request.outcome=confirmed|not_sent` y su directorio se sincronizan antes de
borrar el guard y sincronizar el directorio base.

Timeout, transporte ambiguo, `SIGKILL`, child inválido, estado `RUNNING`, job
ausente/corrupto o fingerprint discordante conservan el guard y bloquean todo
POST futuro sin replay. `doctor` y `job list` lo reportan de forma saneada;
`job purge` rechaza el job referenciado. Nunca se limpia automáticamente sólo
porque el PID murió. Un marker con job aún `CREATED` sí prueba que el proceso
cayó antes de la transición durable `RUNNING` y sólo se reconcilia bajo el lock.

Todos los timeouts float deben ser finitos y estar dentro de su rango. `nan` e
infinito se rechazan antes de esperar el lock, hacer I/O, calcular límites o
iniciar Bubblewrap, y nunca aparecen como números JSON no estándar.

`POST /v1/responses` tiene cero retries. Un 429, timeout, error de transporte o
fallo del proveedor no se reenvía automáticamente porque el resultado remoto
puede ser ambiguo y un replay duplicaría consumo. Solo el GET de modelos usado
por `doctor --probe` o `endpoint probe` puede reintentar.

Esto es protección fail-closed dentro del boot, no garantía remota exactly-once:
Responses no ofrece una idempotency key para este contrato y `/tmp` no
sobrevive un reboot. Una ambigüedad pendiente exige reconciliación de
proveedor/cuenta o una decisión explícita de abandono antes de nuevos POST.

En errores HTTP se lee como máximo un body pequeño y solo se conserva el código
numérico saneado de MiniMax. Así se distinguen rate limit (`1002`), saldo
insuficiente (`1008`) y cuota Token Plan (`2056`) sin persistir el mensaje/body
del proveedor ni reintentar el POST.

## Eficiencia de tokens

- `--reasoning none` es el default; `high` solo para complejidad real o un
  repair acotado.
- La orquestación empieza con `--max-output-tokens 8000` para jobs estrechos y
  eleva el cap una sola vez únicamente cuando el alcance revisado lo exige.
- MiniMax recibe archivos concretos, nunca el repositorio completo.
- El contrato estático y `prompt_cache_key` permanecen estables.
- El supervisor recibe patch y métricas, no razonamiento largo.
- Se registran `input_tokens`, `cached_tokens`, `output_tokens` y
  `reasoning_tokens`.
- El tier permanece `standard`; no se sustituye la clave por una API key
  pay-as-you-go. Los Credits comprados asociados a la Subscription Key pueden
  cubrir overflow según la cuenta y se reportan como riesgo no controlable por
  el request.
- No se llama a `/responses/input_tokens` con jobs privados porque duplicaría
  el envío del bundle. Los caps locales conservadores y el usage real del único
  POST limitan coste, aunque no sustituyen un conteo exacto del tokenizer.

MiniMax documenta caching por prefijo y tokens cacheados:
<https://platform.minimax.io/docs/api-reference/text-prompt-caching>.
El prefijo reutilizable debe alcanzar al menos 512 tokens para ser cacheable.
Las deducciones y ventanas vigentes de 5 horas/semanales son estado de cuenta
del proveedor; el runner registra usage real, pero no estima cuota restante.
Todavía no existe un ledger acumulado sesión/día ni deduplicación durable entre
reinicios; para un presupuesto monetario fail-closed esos controles deben
añadirse fuera de `/tmp` y combinarse con límites de cuenta del proveedor.

## Límites financieros

El Token Plan es un worker de desarrollo, no una dependencia del camino de
ejecución con dinero real ni evidencia o garantía de rentabilidad. MiniMax no
puede:

- validar su propio patch;
- aprobar backtests, evidencia o promoción;
- leer estado broker o secretos;
- activar `live_trading_allowed`;
- colocar, cancelar o reconciliar órdenes.

El sistema permanece paper-only hasta superar por separado todos los gates P0,
la campaña paper, shadow live y la aprobación financiera independiente.
`live_trading_allowed` permanece `false`.

## Canario real no sensible — 2026-07-14

Se ejecutó un único POST real contra MiniMax-M3 usando un repositorio temporal
sintético de 63 bytes, sin código ni datos del proyecto. El spec pedía corregir
una función `add(left, right)` que restaba. Resultado:

```text
job_id = 783229eea2f4ddaefd28
status = PATCH_READY
changed_paths = [src/canary.py]
patch_sha256 = e7a42e173630fba2b2077d6177674b717d615d859e3bc51df12ef4e4497a7784
input_tokens = 543
cached_tokens = 128
output_tokens = 83
reasoning_tokens = 0
total_tokens = 626
```

Codex revisó el manifest sellado y el diff, verificó `git apply --check`, aplicó
el patch sólo al checkout temporal y ejecutó el test con entorno vacío. El
cambio fue exactamente `return left - right` → `return left + right`; el test
pasó y el job se purgó. Esto prueba autenticación, endpoint Responses, formato
de patch y ciclo automático Codex→MiniMax→Codex. No prueba que sea seguro enviar
fuentes privadas, no valida lógica financiera y no autoriza live.

Una revalidación posterior con `doctor --probe` hizo únicamente `GET
/v1/models`, sin transmitir archivos. Confirmó endpoint alcanzable,
`MiniMax-M3` presente en el catálogo, credencial Token Plan tomada de la función
Fish esperada y `worker_tools=[]`. Esa evidencia corresponde a la revisión
instalada anterior. La fuente v0.3.5 queda pinneada al hash nuevo indicado abajo;
su instalación global y la copia privada deben actualizarse y compararse de
forma explícita antes del próximo uso.
La autorización reutilizable queda limitada a ese ejecutable auditado; no
convierte una denegación administrada de red/egress en permiso.
La evidencia histórica del 2026-07-15 registró una regla reutilizable para el
prefijo exacto `/usr/bin/python3 -I
/home/adquiod/.local/bin/minimax-api-worker`; no autorizaba Python genérico,
otros scripts, hosts alternativos ni herramientas del modelo. No se trata como
permiso efectivo actual: las escalaciones de esta sesión fueron rechazadas por
el límite de uso de la plataforma y la copia privada todavía es v0.3.2.

La verificación local del runner v0.3.5 terminó con 78/78 pruebas y SHA-256
`5fdd4dcf32e47ca5c27b169d7472ecd036cd7fcf7b90b0de6d15b4ea43b7498d`.
La suite propia del verificador contiene 33 casos: 30 pasaron y tres perfiles
Bubblewrap permanecen opt-in y se omiten dentro del seccomp administrado. La
ejecución escalada de esos tres casos sobre los hashes actuales fue rechazada
por límite de uso de la plataforma, no por el verificador. En la revisión
anterior esos perfiles confirmaron red
sólo-loopback, HOME/configuración Fish/env/repo real ausentes, `/work` read-only,
venv saneado con `-S` y el ciclo end-to-end con respuesta MiniMax simulada sin
modificar el checkout ni llamar la API. El runner
versionable, `~/.local/bin/minimax-api-worker` y la instalación global activa
deben compartir el mismo SHA-256 después de cada actualización; el hash final
se registra de nuevo tras instalar la copia auditada.
La auditoría encontró una sola asignación de la clave en Fish, ninguna en Zsh,
modo `0600` para el archivo Fish y copia privada no escribible del comando.

## Estado de aceptación

```text
bridge_version = 0.3.5
bridge_design = API_DIRECT_NO_TOOLS
source_review = GO_CONDITIONED_NO_OPEN_P0_P1
installed_bridge = HOLD_OLD_V0.3.2_NO_VERIFIER
patch_verifier = STATIC_AND_SERIAL_PASS_REAL_BWRAP_CURRENT_HASH_PENDING
patch_verifier_version = 0.3.0
worker_sha256 = 5fdd4dcf32e47ca5c27b169d7472ecd036cd7fcf7b90b0de6d15b4ea43b7498d
patch_verifier_sha256 = 361d667f38ecb8f2b9da3debdc01065dbc3505adbba42522e2fee783c8004a48
provider_contract = MINIMAX_M3_TOKEN_PLAN
credit_overflow_guard = EXTERNAL_ACCOUNT_CONTROL_REQUIRED
primary_codex_role = ARCHITECT_REVIEWER
fish_credential_source = FOUND
installed_command_form = REGULAR_COPY_NO_SYMLINK
workflow_automation = SUBJECT_TO_MANAGED_EGRESS_POLICY
local_hardware_requirement = NONE
cloud_repo_transfer_in_this_audit = SYNTHETIC_ONLY_NO_PRIVATE_SOURCE
synthetic_api_canary = PASS
private_canary = POST_ATTEMPTED_DELIVERY_UNCONFIRMED
automatic_delegation_ready = FALSE
live_trading_allowed = FALSE
```

El `doctor` offline ejecutado desde la fuente v0.3.5 confirmó endpoint/model
exactos, `worker_tools=[]`, credencial Fish presente sin imprimirla y guard
global `clear`; `job list` devolvió cero jobs. Esto valida la fuente local, no
la instalación global v0.3.2 ni el egress administrado.

La regresión completa actual ejecutó 1.962 tests bajo la venv Python 3.12:
1.894 pasaron, 34 fallaron exclusivamente porque el seccomp administrado
rechazó `bind(AF_UNIX)` en `test_paper_executor_ipc`, y 34 fueron omitidos (33
por requerir las primitivas memfd/seals del intérprete productivo).
La repetición fuera de ese seccomp fue bloqueada por la cuota administrada. No
se interpreta ese resultado como una regresión funcional ni como suite completa
verde: el gate queda pendiente y se conserva el resultado exacto.

La suite local usa respuestas simuladas; además, el canario sintético anterior
verificó una llamada API real sin transmitir el repositorio. El 2026-07-14 se
intentó después un POST con un bundle privado mínimo compuesto únicamente por
stubs genéricos nuevos y sus pruebas. Terminó en
`provider_unreachable`/`URLError`, sin respuesta, usage ni patch; la entrega no
puede confirmarse. El job se purgó y no se reintentará. Cualquier delegación
privada futura requiere un snapshot nuevo, revisión del manifiesto y una
política activa que permita expresamente ese egress; sigue excluida para
controles financieros, datos, broker, ejecución, riesgo, promoción y deploy.
