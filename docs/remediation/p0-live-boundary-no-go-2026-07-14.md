# Bloqueo de frontera live — NO-GO 2026-07-14

## Veredicto

El sistema **no está autorizado ni preparado para enviar órdenes con dinero
real**. Los flags versionados continúan en `false` y, como contención adicional,
el canary y el adaptador Alpaca live rechazan incondicionalmente cualquier
solicitud de submit antes de llamar al cliente broker.

Este documento no evalúa ni promete rentabilidad. Separa dos preguntas:

1. si existe edge neto, que requiere evidencia P1 todavía ausente; y
2. si la ruta puede mover capital de forma controlada, que todavía falla varios
   requisitos P0.

MiniMax-M3 no intervino en esta revisión ni en las correcciones: configuración,
riesgo, broker, ejecución, reconciliación y promoción son superficies
financieras protegidas y quedan bajo Codex y gates deterministas.

## Fallos confirmados por la reauditoría

Antes de la contención, pruebas adversariales demostraron que:

- `bool("false")` podía convertir una bandera textual en autorización live;
- `NaN`/infinito atravesaban comparaciones de límites y una orden con estado de
  riesgo no finito podía resultar aceptada;
- el canary construía el riesgo con ceros suministrados por el caller, sin leer
  pérdida diaria, drawdown, exposición y posiciones reales de la cuenta;
- un notional enorme podía acompañarse de peso/exposición declarados como cero;
- una respuesta broker rechazada o sin ID podía tratarse como submit exitoso;
- no existían journal durable, límite de órdenes consumible, reconciliación
  post-side-effect ni rollback live real;
- readiness, rehearsal, breaker, autonomía y approval dependían de artefactos
  locales sustituibles y no quedaban unidos a un intent completo de orden;
- breaker, veto y autonomía sufrían una ventana TOCTOU entre comprobación y
  submit;
- conciliación pisaba símbolos duplicados, aceptaba no finitos e ignoraba
  estados de orden desconocidos;
- JSON y YAML centrales aceptaban claves duplicadas y constantes no finitas.

Por tanto, una suite verde anterior demostraba cobertura insuficiente, no
seguridad live.

## Contención implementada

- `live_canary`: toda petición `enable_real_submit=True` recibe
  `real_submit_disabled_pending_p0_controls`; no se construye runtime, no se
  leen credenciales y no se llama al broker.
- `AlpacaLiveBroker.submit_order`: nunca llama al cliente. Incluso con
  `submit_enabled=True` devuelve
  `live_submit_disabled_pending_p0_controls`.
- El adaptador rechaza identidad, cantidades, precios, desviación y contexto de
  riesgo ausentes, booleanos o no finitos. El contexto suministrado por el
  caller se marca además `live_risk_context_unverified`.
- `load_risk_config` exige booleanos reales, números finitos e integers exactos;
  rechaza claves YAML duplicadas y un argumento `allow_live` que no sea bool.
- El JSON común rechaza claves duplicadas y `NaN`/infinito tanto al leer como al
  escribir.
- La política de riesgo usa drawdown positivo `[0,1]`, bloquea estados no
  finitos/rangos imposibles y valida sus propios límites.
- La reconciliación bloquea posiciones duplicadas/no finitas, allowlists
  ambiguas, IDs duplicados, edades inválidas y estados de orden desconocidos o
  terminales dentro de un snapshot de abiertas.
- Los precios live booleanos, no positivos o no finitos ya no se aceptan como
  precio de mercado.

Estas correcciones reducen riesgo de ejecución accidental; no convierten la
ruta en utilizable. El código de submit inseguro fue retirado del canary y el
adaptador permanece deliberadamente sin side effects.

## Requisitos para diseñar una nueva ruta live

No se debe quitar el bloqueador hasta demostrar, como conjunto inseparable:

1. Un servicio ejecutor separado de Codex/LLM, shell y checkout, con credenciales
   aisladas y API mínima de intents firmados.
2. Política canónica versionada, prerregistrada y no sustituible mediante una
   ruta CLI arbitraria; kill switch global independiente.
3. Intent de orden que ligue símbolo, lado, cantidad/notional, precios, hashes
   de todos los artefactos, cuenta, policy, breaker, expiración y nonce de un
   solo uso.
4. Snapshot broker real y atómico de equity, PnL, drawdown, posiciones,
   exposición, órdenes abiertas y buying power; ningún scalar de riesgo
   autodeclarado por el caller.
5. Journal durable broker-first con fencing/lock de cuenta, idempotencia por
   `client_order_id`, límite diario y recuperación de timeout ambiguo antes de
   cualquier retry.
6. Relectura transaccional de breaker, veto, autonomía, policy y aprobación
   inmediatamente antes del side effect.
7. Máquina de estados completa: ACK no es fill; rejected, canceled, partial,
   timeout, overfill, correction y bust deben reconciliarse. El éxito exige
   fills terminales, posición y fees.
8. Rollback live real probado: cancelar abiertas, reducir/flatten idempotente,
   reconciliar después y disparar alertas/breaker ante cualquier incertidumbre.
9. Quotes con símbolo/feed/timestamp/procedencia y umbral de frescura
   prerregistrado; coste/fee real en ledger append-only anclado externamente.
10. Fault injection de crash, pérdida de red, respuesta post-side-effect,
    concurrencia, corrupción y replay; shadow live sin órdenes antes de un nuevo
    diseño de canary.

Eligibility, stage policy y Gate 1 paper no sustituyen estos controles. Aun
cuando queden verdes, el submit live debe permanecer imposible hasta una
revisión explícita del nuevo ejecutor y su threat model.

## Verificación de la contención

La validación final del 2026-07-14 se ejecutó sin red, MiniMax, broker ni
acciones live:

- `1.641` pruebas unitarias/integración aprobadas, sin fallos ni errores;
- Ruff limpio sobre `78` archivos Python modificados o nuevos;
- `git diff --check` limpio;
- sintaxis válida de `scripts/run-live-canary.sh`;
- verificadores estáticos `live` y `futures` aprobados.

Una ejecución verde demuestra que la contención y sus regresiones conocidas
son coherentes; no demuestra seguridad live, edge económico ni rentabilidad.
