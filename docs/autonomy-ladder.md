# Escalera de Autonomia N0-N3

**Fecha:** 2026-07-06
**Autor:** Fable 5 (Arquitecto/Lider Tecnico)
**Ejecutor de implementacion:** Claude Sonnet 5, bajo especificacion y revision del Arquitecto
**Estado:** Diseno aprobado; implementacion por sprints A1-A6

## Proposito

El objetivo final del proyecto es un sistema que opere capital real de forma
autonoma, con el humano como supervisor via Telegram y no como aprobador por
operacion. Este documento define la maquinaria de gobernanza que hace ese
transito seguro: una escalera de niveles de autonomia por mercado, que sube
solo con evidencia certificada y baja sola ante incidentes.

Principio rector: la escalera **agrega** gates sobre la maquinaria existente
(paper stages, live stage policy, circuit breakers, reconciliacion,
kill switch); nunca elimina ni relaja un gate existente. Ningun componente de
la escalera construye clientes de broker ni envia ordenes.

## Niveles

| Nivel | Nombre | Semantica | Evidencia minima para entrar |
|---|---|---|---|
| N0 | `N0_PAPER_AUTO` | Ciclo completo autonomo en paper | Punto de partida (fail-closed) |
| N1 | `N1_REAL_CANARY` | Capital real minimo; humano aprueba cada senal por Telegram | >= 20 dias habiles paper con PnL neto positivo y drawdown en limites |
| N2 | `N2_REAL_SEMI_AUTO` | Ejecuta solo; humano tiene ventana de veto por Telegram | >= 10 dias reales N1 sin incidentes |
| N3 | `N3_REAL_AUTO` | Sin aprobacion ni veto previo; supervision Telegram | >= 15 dias reales N2 con metricas iguales o mejores que paper |

Reglas duras:

- Promocion de un nivel a la vez; saltos de nivel quedan bloqueados.
- Toda promocion exige reviewer humano, reason y hash del artefacto de
  evidencia; queda registrada en ledger append-only.
- Incidente grave (breaker tripped, divergencia de reconciliacion, perdida
  fuera de modelo) degrada un nivel de forma automatica e inmediata; volver a
  subir exige recertificacion completa del nivel.
- Estado ausente, corrupto o con checksum invalido se lee como N0
  (fail-closed), igual que el idioma de `paper_risk_state`.
- La escalera es metadato de gating: los gates de ejecucion la consultan y
  bloquean; el modulo de escalera jamas ejecuta.

## Mercados y secuencia

| Mercado | Clave | Precondicion para iniciar su N0 |
|---|---|---|
| Equities/ETFs (Alpaca) | `equities` | Ninguna; primer mercado |
| Futuros micro | `futures` | `equities` en N2 o superior, `futures_readiness` limpio |
| Forex mayores | `forex` | `futures` en N2 o superior, `forex_readiness` limpio |

Nunca se activa un mercado nuevo con incidentes abiertos en el anterior.
Cada mercado lleva su propio estado y su propia escalera completa N0-N3.

## Mapeo a la maquinaria existente

| Pieza de la escalera | Se apoya en |
|---|---|
| Evidencia N0 | `paper_auto_cycle`, `paper_performance`, `paper_evidence_index`, PAPER_STAGES (`CANARY`/`SCALE_UP`/`READINESS`) |
| Entrada a N1 | `live_readiness` (evidencia, no autorizacion), `live_stage_policy` (`LIVE_CANARY` USD 1), wrapper humano `run-live-canary.sh` con doble confirmacion |
| Aprobacion/veto N1-N2 | `telegram_control` (intents con allowlist, anti-replay, ledger) |
| Escala de capital N2-N3 | `live_stage_policy` (`LIVE_SCALE_UP` USD 50-100), `position_sizing` con bloqueo por edge neto |
| Degradacion automatica | `live_circuit_breaker`, `live_reconciliation`, kill switch de `paper_risk_state` |
| Observabilidad | `paper_telegram_notify/status/history`, `live_observability` |

La progresion de capital documentada en `docs/live-transition-sprints.md`
(paper USD 1 -> paper gobernado -> readiness -> live dry-run -> live canary
USD 1 -> live scale-up USD 50-100) queda intacta; N1-N3 son la capa de
gobernanza que decide cuanta intervencion humana exige cada paso.

## Diseno del modulo `autonomy_level`

Archivo: `src/trading_ai/execution/autonomy_level.py`.

- `AUTONOMY_LEVELS = ("N0_PAPER_AUTO", "N1_REAL_CANARY", "N2_REAL_SEMI_AUTO", "N3_REAL_AUTO")`
- `AUTONOMY_MARKETS = ("equities", "futures", "forex")`
- Estado por mercado en `reports/tmp/autonomy/<market>/state.json` con campo
  `integrity_sha256` (mismo idioma de checksum que `paper_risk_state`).
- Ledger append-only `reports/tmp/autonomy/<market>/ledger.jsonl` con toda
  promocion, degradacion e incidente (timestamp, actor, reason, hashes).
- API:
  - `load_autonomy_state(market, path=...) -> AutonomyState`: fail-closed a N0
    ante ausencia, corrupcion o checksum invalido; el estado degradado por
    lectura fail-closed queda marcado `fail_closed=true`.
  - `certify_autonomy_promotion(market, target_level, evidence, reviewer,
    reason, ...) -> AutonomyDecision`: valida un solo nivel de salto,
    requisitos de evidencia del nivel objetivo, precondicion de secuencia de
    mercados e incidentes abiertos; escribe estado + ledger + artefacto JSON.
  - `record_autonomy_incident(market, severity, source, reason, ...) ->
    AutonomyDecision`: `severity="grave"` degrada un nivel inmediato y deja
    `open_incident=true`; severidades menores solo registran.
  - `evaluate_autonomy_gate(market, requested_action, state) -> blockers`:
    funcion pura que los gates de ejecucion consultan (p. ej. accion
    `real_submit` exige N1+ mas aprobacion, N2+ mas ventana de veto, N3).
- CLI (`trading_ai.cli`): `autonomy-status`, `autonomy-certify`,
  `autonomy-incident`. Solo reportes y estado; sin flags de submit; artefactos
  con el bloque `safety` estandar en false.

## Plan de sprints

| Sprint | Alcance | Estado |
|---|---|---|
| A1 | Modulo `autonomy_level` + CLI + tests (maquina de estados, fail-closed, ledger, secuencia de mercados) | Especificado |
| A2 | Intents de aprobacion/veto de senales en `telegram_control` con vinculo a arbitraje de senales (mecanica N1/N2) | Pendiente de spec |
| A3 | Acumulador de evidencia N0: reporte de certificacion que agrega >= 20 dias de artefactos paper (PnL neto, drawdown, win rate, dias limpios) | Pendiente de spec |
| A4 | Endurecimiento de gestion dinamica TP/SL (trailing, proteccion de ganancias) sobre `paper_position_watch`/`paper_position_plan` + integracion EOD | Pendiente de spec |
| A5 | Cableado de incidentes: eventos de breaker/reconciliacion generan incidentes de autonomia automaticamente | Pendiente de spec |
| A6 | Gate de autonomia en el camino real: `live_execute_session` y wrapper canary consultan `evaluate_autonomy_gate` | Pendiente de spec |

Cada sprint cierra con: `scripts/verify-release-minimal.sh` verde, suite
`unittest` completa verde, scanner de seguridad limpio y revision del
Arquitecto sobre el diff completo. Un error detectado en operacion detiene el
desarrollo de features hasta aislarlo con test de regresion.

## Invariantes de no-regresion

- `PAPER_STAGES` no cambia; `LIVE_STAGES` no cambia.
- Ningun artefacto de la escalera declara autorizacion live; el scanner
  `verify-safety-patterns.py --mode live` debe salir limpio en cada sprint.
- El estado de autonomia nunca sustituye reviewer/reason/doble confirmacion
  del camino live existente; solo agrega una condicion mas.
- Todo cambio de codigo se revalida en paper/shadow antes de tocar caminos
  reales.
