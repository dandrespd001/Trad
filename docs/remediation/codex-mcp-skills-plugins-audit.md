# Auditoría Codex: MCP, Skills y Plugins

Fecha: 2026-07-15. Alcance: configuración global observada en modo read-only;
no se modificaron MCPs ni plugins globales durante esta revisión.

## Veredicto

La integración MiniMax-M3 no debe implementarse como MCP ni plugin. El worker
directo de Responses API tiene menos autoridad, cero tools y un contrato de
egress auditable. El catálogo general de Codex funciona, pero está más amplio y
menos reproducible de lo necesario: conviene reducirlo antes de usar este host
como estación de control financiero.

## Inventario observado

- Codex CLI: `0.144.4`.
- MCPs efectivos: 11; habilitados: 9.
- `alpaca` y `github` MCP están deshabilitados. Mantener `alpaca` así es un
  control obligatorio mientras no exista el executor IPC único.
- Cinco MCPs usan `@latest`: Context7, Memory, Playwright,
  Sequential Thinking y Yahoo Finance. Filesystem tampoco fija versión. Las
  versiones resueltas observadas fueron Playwright `0.0.78`, Filesystem
  `2026.7.10`, Memory/Sequential `2026.7.4`, Context7 `3.2.3` y Yahoo `1.2.2`;
  son evidencia del host, no pins efectivos.
- Filesystem recibe como roots todo `/home/adquiod/Proyectos` y
  `/home/adquiod/Documentos`, muy por encima del workspace actual; únicamente
  `create_directory` declara aprobación explícita en su override local.
- Plugins instalados/habilitados: 6 (`superpowers`, `github`,
  `build-web-apps`, `build-web-data-visualization`, `codex-security` y
  `openai-developers`). Sus caches contienen 57 Skills.
- `gh-address-comments`, `gh-fix-ci` y `yeet` existen tanto en la instalación
  de Skills personales como dentro del plugin GitHub. La copia personal de
  `openai-docs` está deshabilitada correctamente para preferir la vigente.
- MiniMax no aparece como MCP o plugin; existe una sola Skill primaria en
  `~/.agents/skills/delegate-minimax-api`.

## Riesgos y coste operativo

1. `@latest` vuelve no reproducibles arranque, permisos y superficie de
   herramientas; una publicación upstream puede cambiar comportamiento sin un
   cambio versionado local.
2. Filesystem amplía lectura/escritura potencial a dos árboles personales. En
   una sesión comprometida o ante una selección errónea, el radio de impacto es
   mucho mayor que el repositorio abierto.
3. Cada MCP y Skill adicional aumenta tiempo de arranque, inventario de tools,
   contexto de selección y probabilidad de elegir una capacidad equivocada.
4. Skills duplicadas crean precedencia ambigua y mantenimiento doble.
5. Yahoo Finance no es una fuente aprobada de ejecución ni evidencia de broker;
   Memory no debe conservar secretos, snapshots broker o decisiones de
   promoción.

## Remediación propuesta

### P0 — frontera financiera

- Mantener `alpaca` MCP deshabilitado y retirar el launcher con credenciales del
  camino operativo. Si se conserva para diagnóstico, reemplazarlo por un proxy
  atestado GET-only, sin submit/cancel, detrás del executor de cuenta.
- Prohibir datos broker, riesgo, promoción, secretos y órdenes en Memory o en
  cualquier MCP de terceros.

### P1 — mínimo privilegio y reproducibilidad

- Deshabilitar Filesystem por defecto o limitarlo al workspace actual. Codex ya
  dispone de filesystem nativo gobernado por sandbox; duplicar una raíz amplia
  por MCP no aporta una frontera adicional.
- Fijar cada paquete MCP a una versión exacta y registrar hash/versión probada.
  Actualizar mediante un cambio explícito con `doctor`, smoke test y rollback.
- Crear perfiles por tarea: núcleo mínimo por defecto; Playwright/UI, docs,
  finanzas auxiliares y razonamiento se habilitan solo cuando el trabajo los
  necesita.

### P2 — catálogo de Skills/Plugins

- Elegir una sola procedencia para `gh-address-comments`, `gh-fix-ci` y `yeet`:
  plugin GitHub o Skills personales, no ambas.
- Deshabilitar `build-web-apps` y `build-web-data-visualization` fuera de tareas
  UI; conservar `codex-security` y `openai-developers` solo si sus MCPs/Skills
  se usan regularmente.
- Mantener una tabla versionada con nombre, procedencia, versión, tools, acceso
  de red, acceso a secretos y responsable de cada MCP/plugin.

## Criterio de aceptación

- Cero `@latest` en MCPs habilitados.
- Ningún filesystem MCP con una raíz superior al workspace necesario.
- Cero Skills activas duplicadas por nombre.
- Alpaca MCP sin credenciales y sin métodos mutables, o deshabilitado.
- Inventario mínimo verificado por `codex mcp list`, `codex plugin list` y un
  smoke test sin red ni secretos.

Estos cambios globales deben ejecutarse en una ventana separada: pueden retirar
capacidades usadas por otros proyectos y requieren reiniciar Codex para medir el
catálogo efectivo. No son requisito para el worker MiniMax API v0.3.5, que no
depende de MCPs ni plugins. La fuente v0.3.5 quedó cubierta por 78 pruebas del
worker y 33 del verificador: 108 pasaron bajo `/usr/bin/python3 3.14.6` y tres
perfiles Bubblewrap reales quedaron opt-in. Su ejecución exacta fue bloqueada
por el límite de uso de la plataforma, no por un fallo de código.

La instalación activa no coincide todavía con esa fuente: la Skill global y el
comando privado siguen en runner v0.3.2, SHA-256 `76adff94...`, y la instalación
global carece del verificador. Por tanto el sistema permanece `HOLD` para
delegación automática. Tampoco se empaquetarán los tres `.pyc` observados bajo
`skills/delegate-minimax-api/scripts/__pycache__`; su limpieza y la instalación
global fueron bloqueadas por el mismo límite administrado. Toda la integración
continúa sin seguimiento Git, de modo que aún falta una release/commit aprobado
o un artefacto inmutable equivalente.
