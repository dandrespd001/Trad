# Threat Model & Quant/Risk Review — Algoritmic-IA

> Nota de reconciliacion (2026-06-25): este documento conserva el reporte historico
> recibido el 2026-06-24. La auditoria offline posterior contrasto estos hallazgos
> contra el codigo actual; varios puntos ya estaban mitigados o no eran alcanzables
> en el estado vigente del repositorio. Ver `docs/audit-2026-06-25-offline.md` para
> la decision reconciliada.

**Repo:** `/home/adquiod/Documentos/Algoritmic-IA`
**Mode audited:** Paper trading (Alpaca, `paper=True` hardcoded)
**Deployment (per operator):** Contenedor / cloud
**Live horizon (per operator):** Corto plazo (semanas/meses)
**Access model (per operator):** Operador único
**Date:** 2026-06-24
**Reviewer:** Auditor senior (quant/risk + ingeniería de sistemas de baja latencia)

---

## 0. Resumen ejecutivo

El sistema es un **scaffold paper-only hacia Alpaca** inusualmente disciplinado: `paper=True` es el único constructor de cliente, confirmación de doble llave en los 5 sitios que envían órdenes, kill-switch latching, CI que falla si alguien reactiva `live_trading_authorized`, sin secretos comprometidos en git, sin logging de credenciales. La postura de contención de paper→live es robusta.

**Sin embargo, tres hallazgos son críticos** y todos comparten la misma causa raíz: **los controles de riesgo están diseñados para una máquina local persistente, pero el sistema se desplegará en contenedor/cloud con transición a live inminente.**

1. **CRÍTICO — El kill-switch "latching" no es latching bajo cloud.** El estado vive en `reports/tmp/paper_risk_state.json` (FS local). Un restart/reschedule de contenedor sin volumen persistente lo pierde → el switch se resetea a `False` silenciosamente. `load_risk_state` además **falla abierto** (devuelve estado limpio) ante archivo corrupto/inexistente. En live, esto significa que un drawdown que debió detener el trading **no detiene nada** tras un redeploy.
2. **CRÍTICO (para live) — Sin gate de horas de mercado ni sanity de precio.** El calendario NYSE existe pero solo valida datos; el path de órdenes no lo usa. Una orden se puede enviar en fin de semana o con un precio de mercado corrupto sin guardia previa.
3. **ALTO — La función objetivo declarada (retorno ajustado por riesgo) no existe en ejecución.** Sharpe/Sortino/vol-target son solo umbrerles de promoción de modelos. En trade time no hay optimización riesgo-retorno; el sizing es `fixed_notional` fijo ($1.00 CANARY).

> **Recomendación principal:** no proceder a live hasta cerrar los hallazgos #1 y #2. Son los únicos que, en paper, son tolerables pero que en live convierten controles declarados en controles ilusorios.

---

## 1. Ámbito y modelo del sistema

### Componentes (con evidencia)
| Componente | Archivo | Notas |
|---|---|---|
| CLI / orquestador diario | `src/trading_ai/cli.py:192`, `paper-auto-cycle` | Entrada del operador |
| Generación de señales | `models/signals.py:41-66` | Logístico, umbral 0.5 |
| Arbitraje LLM↔baseline | `execution/paper_signal_arbitration.py` | Emite `ELIGIBLE_FOR_PAPER`/`BLOCKED` |
| Construcción offline de orden | `paper_session` (dry-run, hash-checked notional) | Intención de orden firmada |
| **Camino de ejecución real** | `execution/paper_execute_session.py:64` | Único path con `dry_run=False` real |
| Broker boundary | `execution/alpaca_paper.py:146` (`AlpacaPaperBroker`) | Gates + submit + reconcile |
| Límites de riesgo | `risk/policy.py:8-78` (`RiskLimits`, `evaluate_risk_state`) | |
| Estado de riesgo + kill-switch | `execution/paper_risk_state.py` | JSON en FS local |
| Sizing | `execution/position_sizing.py` | `fixed_notional` |
| Datos de mercado | `data/market_data.py` (`ApprovedLocalMarketDataProvider`) | **100% local, sin red** |
| Notificaciones | `execution/paper_monitor.py:342-361` (Telegram) | webhook saliente |
| Calendario NYSE | `data/market_calendar.py:101` | **No se usa en ejecución** |

### Camino de orden (signal → fill)
```
señal (logística) → arbitraje → paper_session (intent, hash-checked $1.00)
  → paper_execute_session [requiere --confirm-paper + --confirm-submit]
    → load risk_state → roll_daily_equity → evaluate_kill_switch
    → compute_order_risk_inputs (4 inputs)
    → evaluate_paper_preflight (stale/orden abierta/dup/posición)
    → AlpacaPaperBroker.submit_order:
        kill_switch → dup_id → allowlist → side/qty/notional → evaluate_risk_state → submit
    → get_order_by_client_id (confirmación/idempotencia)
```

### Fuera de ámbito (excluido explícitamente)
- Tooling de dev/CI (`scripts/verify-*.sh`, `.github/workflows/` como activos, no como runtime).
- Modelos de research (`src/trading_ai/research/`, `backtest/`) salvo donde definen umbrales de promoción consumidos en vivo.
- El contenido real de `.env` (no se evalúa valor de secretos; solo su manejo).

---

## 2. Límites de confianza, activos y puntos de entrada

### Límites de confianza (4)
| # | Borde | Protocolo | Auth | Encriptación | Validación | Rate-limit |
|---|---|---|---|---|---|---|
| B1 | Proceso → Alpaca Paper API | HTTPS (SDK `alpaca-py`) | API key+secret (env-var) | TLS (SDK) | Sí (allowlist, gates) | Retry backoff (`:14`) |
| B2 | Proceso → Telegram API | HTTPS (`urllib`) | Bot token (env-var) | TLS | Ninguna en payload | No |
| B3 | FS local → estado de riesgo | FS (JSON) | **Ninguna** | **Ninguna** | Sí (parseo) | N/A |
| B4 | (Inexistente) Proceso → Broker live | Bloqueado por diseño | — | — | — | — |

### Activos que mueven el riesgo
- **A1 — Credenciales Alpaca** (env-var):读写 de órdenes/posiciones/cuenta. Papel hoy, **capital real mañana**.
- **A2 — Estado de riesgo persistido** (`reports/tmp/paper_risk_state.json`): controla el kill-switch y los inputs de los gates. **Integridad = integridad del control de riesgo.**
- **A3 — Config de límites** (`configs/risk.yml`, `permissions.yml`): define max-loss, dd, exposure, allow_live.
- **A4 — Token Telegram** (env-var): canal de alertas (exfil posible).
- **A5 — Integridad del paquete de orden aprobado** (hash del notional): anti-tamper del sizing.
- **A6 — Decide live/no-live**: `live_trading_allowed` + constructor `paper=True` + flags CI.

### Puntos de entrada
- **E1** — CLI del operador (`python -m trading_ai.cli`), vía contenedor/cloud.
- **E2** — `paper_execute_session` con doble confirmación (el único path de submit real).
- **E3** — `paper_safe_flatten --confirm-flatten` (flatten de emergencia).
- **E4** — FS: lectura/escritura de `paper_risk_state.json` (sin auth).
- **E5** — Variables de entorno del contenedor (inyección de creds).

### Capacidades del atacante (realistas, calibradas a operador único + cloud)
- **Insider malicioso:** descartado por modelo de operador único confiable.
- **Operador que comete un error de configuración en el despliegue:** **alta** (volumen no persistente, env mal inyectada, imagen con secretos en layer).
- **Código/dependencia comprometida (supply-chain):** media-baja (deps con bounds, pero `timesfm`/`chronos-forecasting` jóvenes; `pip-audit` en dry-run).
- **Compromiso del runtime del contenedor:** media (si el host cloud se compromete, A1/A2/A4 quedan expuestos; el FS del estado no tiene integridad).
- **No-capacidades (para no inflar severidad):** no hay superficie web, no hay auth multi-tenant, no hay input externo no-confiable al sistema (las señales son locales).

---

## 3. Amenazas como caminos de abuso (priorizadas)

### T1 — CRÍTICO: El kill-switch latching se resetea silenciosamente en cloud (fallo del control más crítico)
**Camino:** contenedor restart/reschedule sin volumen persistente → `reports/tmp/` vacío → `load_risk_state` (`paper_risk_state.py:99-100`) devuelve `RiskState()` con `kill_switch_active=False` → el broker acepta nuevos opens aunque el switch hubiera tripeado por drawdown/error-streak el día anterior → **trading se reanuda sin intervención humana**.
**Impacto:** integridad del control de riesgo (A2). En live, capital expuesto después de un evento que debió detener todo.
**Likelihood:** **alta** — los restarts son rutinarios en cloud; el código *por diseño* asume FS persistente (docstring `:211` "stays active until an operator explicitly resets it").
**Agravante — fail-open:** incluso con volumen, si el JSON se corrompe, `load_risk_state:101-106` devuelve estado limpio en vez de estado conservador. Es fail-open en un control de safety.
**Agravante — sin integridad:** el JSON no tiene firma/checksum; un proceso (o un bug) que escriba `kill_switch_active:false` lo desarma sin trazabilidad.
**Prioridad:** **CRÍTICO** (likelihood alta × impacto alto, sin mitigación existente).
**Asume:** despliegue cloud donde `reports/tmp/` no es un volumen persistente por defecto. **A confirmar en la configuración real del contenedor.**

### T2 — CRÍTICO (live): Sin gate de horas de mercado en el camino de órdenes
**Camino:** ciclo diario corre en sábado/feriado o fuera de RTH → `submit_order` no verifica `is_trading_day` ni `market_open` → se envía una orden market con `time_in_force=day` (`alpaca_paper.py:497`) → Alpaca la rechaza o la cola.
**Impacto:** en paper, ruido operativo (órdenes pendientes que la reconciliación trata "suave" para opens — `paper_execute_session.py:805-833`). **En live:** órdenes ejecutadas a precios de mercado cerrado/illíquido, o error-streak que dispara el kill-switch (que además podría resetearse, ver T1).
**Likelihood:** **alta** (el calendario existe y *no* está conectado; cron mal puesto es común).
**Evidencia del gap:** `grep` confirma cero uso de `is_trading_day`/`market_hours` en `execution/`; solo en `data/validation.py:11`.
**Prioridad:** **CRÍTICO para live**, medio para paper.

### T3 — ALTO (live): Sin sanity check de precio en la entrada de la orden
**Camino:** un dato de mercado corrupto/extremo (split no ajustado, outlier, feed stale) → señal dispara buy → `submit_order` recibe un `PaperOrder` que **no tiene campo de precio** (`alpaca_paper.py:39-71`: solo symbol/side/qty/notional) → market order enviada sin verificar que el precio de referencia es plausible → fill a precio aberrante.
**Impacto:** pérdida financiera directa en live. En paper, PnL distorsionado que puede promover/retener modelos incorrectos.
**Likelihood:** media (requiere dato malo; pero el feed es local/manual, así que el vector es un CSV mal cargado o feature stale no capturado por preflight).
**Prioridad:** **ALTO para live**, medio-bajo para paper.

### T4 — ALTO: La función objetivo declarada (retorno ajustado por riesgo) no existe en ejecución
**Camino:** el sistema publica como objetivo "optimizar retorno ajustado por riesgo". Pero en trade time: sizing = `fixed_notional` fijo (`position_sizing.py`), selección = umbral logístico 0.5, sin optimización riesgo-retorno. Sharpe/Sortino/vol-target solo son umbrales de **promoción offline** (`configs/risk.yml:25-32`).
**Impacto:** el sistema **no cumple su objetivo declarado**. El riesgo ajustado es un filtro de modelos, no un objetivo de trading. Esto es un defecto de diseño de producto/quant, no de seguridad.
**Likelihood:** cierto (es el estado actual).
**Prioridad:** **ALTO** como desviación de objetivo; no bloquea paper pero **debe definirse antes de escalar capital** (¿cuál es la política de sizing real? ¿Kelly fraccional? ¿vol-target?).

### T5 — MEDIO: `load_risk_config(allow_live=True)` es un escape hatch latente
**Camino:** un futuro caller pasa `allow_live=True` + config con `live_trading_allowed` en `true` → `evaluate_risk_state` deja de emitir `live_trading_not_authorized` (`policy.py:52-53`) → se silencia un gate. No habilita live por sí solo (constructor sigue `paper=True`), pero debilita la postura.
**Impacto:** debilita defensa en profundidad (A6).
**Likelihood:** baja hoy (cero callers); media como deuda técnica futura.
**Prioridad:** **MEDIO**. Mitigación: hacer `allow_live` no-defaultable o require-context-manager, o eliminar el parámetro.

### T6 — MEDIO: Redacción de secretos incompleta para objetos broker serializados
**Camino:** excepción de broker cuyo `repr()` embebe un secreto no presente en env → `redact_secrets` (`paper_common.py:100-109`) solo scrubbea valores de env → depende del fallback regex (`:110-118`) que no cubre formatos arbitrarios → el secreto llega al artefacto JSON/markdown → exfil vía Telegram o FS.
**Impacto:** exfil de credencial (A1/A4). En paper, blast radius limitado (paper key); en live, crítico.
**Likelihood:** baja (requiere secreto en `repr` de respuesta de Alpaca, improbable pero no imposible).
**Prioridad:** **MEDIO para live**.

### T7 — BAJO: Secretos en `.env` local + supply-chain de deps jóvenes
- `.env` existe, permisos `600`, **NO comprometido en git** (verificado: `git log --all -p -S '<valor>'` = 0 commits; solo el *nombre* de la variable aparece en 4 commits sin valor). Rotación **opcional por higiene**, no obligatoria.
- `timesfm`, `chronos-forecasting` (`pyproject.toml:56-57`) son jóvenes; `pip-audit` corre en `--dry-run`.
**Prioridad:** **BAJO**.

### T8 — BAJO: PnL derivado de equity, sin atribución por posición
**Camino:** daily-pnl/drawdown se calculan de equity vs `opening_equity`/`peak_equity` (`paper_risk_state.py:164-165`), no de PnL realizado/no-realizado por posición. Un error de feed en un símbolo puede enmascarar pérdidas reales si el equity agregado no lo refleja a tiempo.
**Prioridad:** **BAJO** hoy (CANARY $1); **MEDIO** al escalar capital.

---

## 4. Revisión quant/risk (resumen de gaps)

| # | Gap | Estado | Crítico para |
|---|---|---|---|
| Q1 | Sin función objetivo riesgo-retorno en vivo (T4) | Ausente | Escalar capital |
| Q2 | Sin gate de horas de mercado (T2) | Calendario existe, no conectado | Live |
| Q3 | Sin sanity de precio en entrada (T3) | `PaperOrder` sin campo precio | Live |
| Q4 | Kill-switch no persiste en cloud + fail-open (T1) | Diseñado para FS local persistente | Live |
| Q5 | PnL equity-derived, sin atribución (T8) | Aceptable en CANARY | Escalar capital |
| Q6 | Sizing vol-target no cableado end-to-end | Fallback a `fixed_notional` por diseño | Definir antes de escalar |
| Q7 | Sells bypass gates diarios/dd por diseño | **Correcto** (de-risking no debe atraparse) | N/A — documentar |

---

## 5. Mitigaciones existentes (con evidencia — fortalezas)

| Control | Evidencia | Calidad |
|---|---|---|
| `paper=True` único constructor | `alpaca_connection.py:56` | Fuerte |
| Confirmación doble llave (5 sitios) | `paper_execute_session.py:75`, `paper_safe_flatten.py:65`, etc. | Fuerte |
| Kill-switch latching en FS | `paper_risk_state.py:182-235` | **Débil en cloud (T1)** |
| CI anti-live (`verify-safety-patterns.py`) | `scripts/verify-safety-patterns.py:19-29` | Fuerte |
| Idempotencia por `client_order_id` + post-timeout lookup | `alpaca_paper.py:176-204,315-328` | Fuerte |
| Notional hash-checked (anti-tamper) | `paper_execute_session.py:569-601` | Fuerte |
| Allowlist de símbolos (10 ETFs) | `configs/universe.yml`, `config.py:40-61` | Fuerte |
| Retry con backoff en códigos transitorios | `alpaca_paper.py:14,176-203` | Fuerte |
| `redact_secrets` en paths de error | `paper_common.py:100-119` | Buena (con caveat T6) |
| Datos 100% locales, sin feed externo | `market_data.py` (`network_used=False`) | Fuerte |
| Sin `logging`, sin `print` de secretos | grep en `src/` | Fuerte |
| Graduation gate (30+ días limpios para escalar) | `paper_graduation.py:283-296` | Fuerte |

---

## 6. Recomendaciones (ancladas a ubicación)

### Bloqueantes para paper→live
1. **(T1) Hacer el kill-switch cloud-resilient y fail-closed.**
   - `paper_risk_state.py:99-106`: ante archivo faltante/corrupto, devolver un estado **conservador** (kill_switch_active=True con reason `state_unavailable_fail_closed`), no limpio. Es inversión de política: fail-closed en controles de safety.
   - Montar `reports/tmp/` como **volumen persistente** en el manifiesto del contenedor. O mejor: migrar el estado a un store gestionado (KV/DDB/secret manager) con TTL y lock.
   - Añadir **integridad** al JSON (HMAC o, mínimo, checksum + `updated_at` + validación de schema estricta).
2. **(T2) Conectar el calendario al path de ejecución.** En `AlpacaPaperBroker.submit_order` (antes del allowlist) o en `paper_execute_session` (antes del submit): rechazar con `market_closed` si `not is_trading_day(today)` o fuera de la ventana RTH configurada. El calendario ya existe (`market_calendar.py:101`); falta una capa de *market hours* (open/close) además de trading-day.
3. **(T3) Añadir sanity de precio.** Añadir `reference_price`/`max_price` a `PaperOrder` y validar en `submit_order` que el precio de mercado actual esté dentro de una banda (ej. ±X% vs el precio del signal pack, o rango histórico plausible). Requiere leer un precio pre-envío.

### Recomendadas (no bloqueantes)
4. **(T4) Definir y documentar la política de sizing/objetivo.** Decidir explícitamente si la meta es vol-target, Kelly fraccional, o risk-parity; si es `fixed_notional` para siempre, **cambiar el objetivo declarado del sistema** para que el artefacto no contradiga la realidad. Cablear `volatility_target_weight` end-to-end si se quiere riesgo-ajustado real.
5. **(T5) Endurecer `allow_live`.** Quitar el default o requerir un context-manager/token explícito; añadir test que falle si algún caller lo pasa.
6. **(T6) Endurecer redacción.** Serializar respuestas de broker con un allowlist de campos en vez de `repr()` libre; o aplicar `redact_secrets` también a subcadenas largas tipo base64.
7. **(T7) Rotar creds Alpaca por higiene** (no urgente; no comprometidas); activar `pip-audit` networked en CI; pinear o auditar `timesfm`/`chronos`.
8. **(T8) Añadir atribución de PnL por posición** antes de escalar capital.

---

## 7. Supuestos y preguntas abiertas

**Supuestos que mueven el ranking (confirmados por el operador):**
- Despliegue en contenedor/cloud → T1 es CRÍTICO, no medio.
- Transición a live en semanas/meses → T2/T3 son bloqueantes, no opcionales.
- Operador único confiable → se descartan amenazas insider/PRC malicioso.

**Supuestos técnicos (a confirmar):**
- **A-C1:** El manifiesto del contenedor monta `reports/tmp/` como volumen persistente. **Si NO lo hace, T1 es CRÍTICO confirmado. Si SÍ, T1 baja a ALTO (queda el fail-open + falta de integridad).** → *Pregunta al operador: ¿cómo está declarado el volumen en el despliegue cloud?*
- **A-C2:** Las creds Alpaca se inyectan como env-var del contenedor (no bakeadas en layer de imagen). → *Confirmar método de inyección de secretos.*
- **A-C3:** El feed de datos manual-csv se valida antes de entrar al sistema (preflight de features mitiga stale, pero no outliers de precio).

---

## 8. Quality check (pre-finalización)

- ✅ Todos los entrypoints cubiertos: CLI (E1), execute-session (E2), safe-flatten (E3), FS estado (E4), env (E5).
- ✅ Cada límite de confianza representado en amenazas: B1→T3, B2→T6, B3→T1 (el más crítico), B4→T5.
- ✅ Separación runtime vs CI/dev: CI tratado como control, no como activo runtime.
- ✅ Clarificaciones del operador reflejadas (deployment, horizon, access).
- ✅ Supuestos y preguntas abiertas explícitos (A-C1, A-C2, A-C3).

---

## 9. Discrepancia entre agentes detectada y resuelta

> **Nota de auditoría:** Un agente de exploración reportó que el secreto de Alpaca "estaba comprometido en el historial git (16 matches)". **Verificado por el auditor directamente: FALSO.** `git log --all -p -S '<valor-del-secreto>'` = 0 commits; solo el *nombre* de la variable aparece en 4 commits (plantillas), sin valor. El secreto **no está comprometido**. La afirmación del agente era una imprecisión (`-S` con nombre ≠ compromiso del valor). Se documenta como lección de método: verificar afirmaciones de severidad crítica con comandos propios antes de propagarlas.
