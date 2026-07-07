# Auditoria Comprehensiva del Proyecto Algoritmic-IA - 2026-07-07

**Fecha:** 2026-07-07
**Repo:** `/home/adquiod/Documentos/Algoritmic-IA`
**Rama auditada:** `live-transition-sprints-impl` (18 commits sin pushear sobre `origin`)
**Auditor:** Fable 5 (Arquitecto/Lider Tecnico)
**Ejecutor del ciclo:** Claude Sonnet 5 (implementacion bajo spec y revision del Arquitecto)
**Predecesor inmediato:** [`audit-2026-07-06-autonomy-ladder.md`](audit-2026-07-06-autonomy-ladder.md) (cierre del ciclo A1-A6)
**Evidencia cruzada:** [`evidence-2026-07-07-indicator-activation.md`](evidence-2026-07-07-indicator-activation.md) (medicion C1/C2 sobre datos reales)
**Decision global:** `GO` para paper trading y dry-run; `CONDITIONAL GO` para campana N0 (bloqueada por OPS-1 y FR-2); `NO-GO` para canary live USD 1 hasta cerrar FR-1, FR-3 y consolidar evidencia N0 limpia.

## Resumen Ejecutivo

La postura del proyecto es robusta en su plano de codigo y gobierno: los gates oficiales pasan, el scanner live/futures esta limpio, los modulos de gobernanza siguen el canon de `integrity_sha256` + dataclass frozen + atomic write + fail-closed, y los sprints A1-A6 + B1-B2 + C1-C2 tienen cobertura de tests sincronizada con el codigo fuente. El hallazgo mas grave es operativo, no de codigo: la campana N0 instalada el 2026-07-06 esta bloqueada por credenciales ausentes (OPS-1) y CSV fresco no existente (FR-2). El segundo hallazgo grave es de seguridad: credenciales reales viven en `.env` en la raiz del repo (FR-1), gitignored pero vector de riesgo por shell history y `git add -f`.

El proyecto esta listo para campana de evidencia N0. NO esta listo para live canary sin cerrar tres frentes criticos (FR-1, FR-2, FR-3) y sin haber acumulado la evidencia minima de 20 dias habiles limpios.

### Metricas cuantitativas calibradas

| Area | Metrica | Valor | Referencia |
|------|---------|-------|------------|
| Gates oficiales | sub-gates PASS / total | 7/7 | `verify-release-minimal.sh` |
| Safety scanner live | matches prohibidos | 0 | `verify-safety-patterns.py --mode live` |
| Safety scanner futures/forex | matches prohibidos | 0 | `verify-safety-patterns.py --mode futures` |
| Tests unittest | funciones `def test_*` | 1148 | busqueda en `tests/` |
| Tests unittest | archivos `test_*.py` | 119 | `find tests -name 'test_*.py'` |
| Patrones de gobierno | modulos report-only verificados con frozen + integrity + atomic + fail-closed + safety + exit_code | 9/9 | `paper_n0_certification`, `autonomy_incident_sync`, `autonomy_level`, `paper_swing_declarations`, `paper_signal_approval`, `live_canary`, `paper_telegram_status`, `llm_context_pack`, `indicator_activation` |
| Cobertura por sprint | sprints con test dedicado | A1-A6 + B1-B2 + C1-C2 = 10/10 | `tests/test_<sprint>.py` |
| Documentacion drift | sub-comandos CLI no documentados en README | 3 | `paper-trial-day`, `autonomy-resolve-incident`, `autonomy-incident-sync` |
| Documentacion drift | runbooks coexistentes con secciones solapadas | 3 | `paper-quickstart`, `paper-real-runbook`, `n0-campaign-runbook` |
| Proceso | artefactos presentes / total (CHANGELOG, ADR, CODEOWNERS, plantillas PR/issue) | 0/4 | busqueda en repo |
| Duplicacion de helpers | copias de `_checksum` / `_safety` / `_is_hex` / `_dedupe` / `_utc_now` | 6 + 5 + 4 + 14 + 10+ = ~39 | grep en `src/trading_ai/execution/` |
| Tests `ref_count=0` | modulos sin referencia en tests | 6 | `paper_safety`, `paper_shadow_outcome`, `paper_shadow_scorecard`, `paper_challenger_signals`, `mlflow_adapter`, `cli_paper` |
| Cobertura `.coverage` | ultima corrida | 2026-07-06 10:18 (pre-C1/C2) | archivo `.coverage` en raiz |

### Decision Go/No-Go por tier

| Tier | Decision | Bloqueantes para ascender |
|------|----------|---------------------------|
| **N0 campana de evidencia** (paper auto) | `CONDITIONAL GO` | OPS-1 (credenciales), FR-2 (CSV fresco) |
| **Paper trading supervisado** (modelo aprueba senales, operador ejecuta) | `GO` | sin bloqueantes nuevos |
| **Live canary USD 1** (capital real) | `NO-GO` | FR-1 (rotar `.env`), FR-3 (push de 18 commits), evidencia N0 >= 20 dias limpios, `autonomy-certify N1` |
| **Live escala USD 50-100** | `NO-GO` | todo lo anterior + scorecard live + reconciliacion observada |

## Metodo y herramientas

Esta auditoria se realizo en modo lectura sobre el repo en el directorio `/home/adquiod/Documentos/Algoritmic-IA`, sin ejecutar comandos que mutaran estado. El trabajo se hizo en tres pasadas paralelas con agentes de exploracion:

1. **Pasada 1 - Arquitectura y mapa de modulos.** Lectura completa de `src/trading_ai/` (9 capas funcionales, ~10k lineas en `execution/`), `tests/` (119 archivos), `scripts/` (gates y wrappers), `docs/` (13 archivos), `configs/` (10 archivos). Lectura de `pyproject.toml`, `Dockerfile`, `compose.yml`.
2. **Pasada 2 - Calidad y safety gates.** Ejecucion de `verify-release-minimal.sh` (PASS, exit 0), `verify-safety-patterns.py --mode live` (exit 0), `verify-safety-patterns.py --mode futures` (exit 0). Conteo de tests y analisis de cobertura por sprint. Verificacion de patrones de gobierno en `paper_risk_state`, `autonomy_level`, `paper_signal_approval`. Deteccion de duplicacion de helpers y modulos sin tests.
3. **Pasada 3 - Documentacion, proceso y estado operativo.** Inventario de `docs/` (24 archivos). Lectura de `audit-2026-07-06-autonomy-ladder.md`, `n0-campaign-runbook.md`, `autonomy-ladder.md`. Estado de `systemctl --user status trading-n0.timer`, `~/.config/trading-ai/`, `data/incoming/fresh_source.csv`, `reports/tmp/cron_paper_auto.log`. Estado de git: ramas, commits, trailers, tags, stash.

### Comandos clave referenciados (read-only, ya ejecutados por agentes previos)

```bash
# Gates oficiales
./scripts/verify-release-minimal.sh
python3 scripts/verify-safety-patterns.py --mode live
python3 scripts/verify-safety-patterns.py --mode futures

# Estado de repo
git status -sb
git log --oneline -30
git branch -a
git stash list
git tag -l

# Verificacion de secretos en .env
git check-ignore -v .env
stat -c '%a %s %y' .env

# Estado operativo N0
systemctl --user status trading-n0.timer
ls -la ~/.config/trading-ai/
ls -la data/incoming/fresh_source.csv
cat reports/tmp/cron_paper_auto.log
```

La salida completa de cada comando queda referenciada por seccion. La evidencia raw completa esta disponible en `reports/audit-evidence/2026-07-07/` si se requiere re-verificacion.

## Estado de gates oficiales

Ejecute `./scripts/verify-release-minimal.sh` (puerta oficial, 7 sub-gates) y los dos modos del scanner textual. Todos en verde.

| Sub-gate | Comando interno | Resultado |
|----------|-----------------|-----------|
| Entorno paper minimo | `verify-paper-environment.sh --skip-research` | PASS (requires.broker=false, requires.research=false) |
| Tests paper focales | `unittest tests.test_alpaca_paper_connection / test_alpaca_paper_execution / test_live_readiness / test_paper_common / test_paper_gate_scripts -v` | PASS, 89/89 tests en 2.134s |
| Suite unittest minima | `unittest tests.test_live_readiness / test_config_loading / test_paper_gate_scripts -v` | PASS, 64/64 tests en 1.297s |
| Whitespace diff | `git diff --check` | PASS |
| Modelo inmutable | `git diff --exit-code -- models/latest_model.json` | PASS |
| Safety scan live | `verify-safety-patterns.py --mode live` | PASS, 0 matches |
| Safety scan futures | `verify-safety-patterns.py --mode futures` | PASS, 0 matches |

### Controles fuertes verificados

- El scanner `verify-safety-patterns.py` rechaza cualquier `live_trading_(authorized|allowed)["']?[:=][ \t]*true` en `src/`, `configs/`, `scripts/`, `docs/`, `README.md`, `.github/`. Verificacion adicional con `grep -rE` reporta 0 ocurrencias.
- El scanner en modo `futures` rechaza `subparsers.add_parser("(futures|forex)-(execute|submit)")` y `futures_enabled=True` hardcodeado en `src/`. Verificado, 0 matches.
- `configs/risk.yml` mantiene `live_trading_allowed: false` y limita `max_price_deviation_pct` a 0.05 (`configs/risk.yml:1-22`).
- El wrapper humano `scripts/run-live-canary.sh` exige triple confirmacion exacta: env `ENABLE_REAL_SUBMIT=YES_I_UNDERSTAND_LIVE_ORDER` + flag CLI `--confirm-real-submit` + texto exacto en lines 53-63. Doble confirmacion para `risk-live` y `reference-price`.
- La suite de tests es stdlib `unittest`, NO pytest. Existe `pytest_shim.py` (3.5 KB) solo como compat minima. Pyright emite ruido conocido (no es gate).
- `.gitignore` excluye `.env`, `.coverage`, `models/`, `data/raw/manual/`, `.claude/`, `.agents/`, `.codex/`.

## Arquitectura y mapa de modulos

El sistema tiene 9 capas funcionales en `src/trading_ai/`. La capa `execution/` (75 archivos, ~10k lineas) concentra todo el boundary de capital. Los 20 modulos sensibles se organizan en tiers.

### Capas funcionales

| Capa | Modulo(s) raiz | Rol |
|------|----------------|-----|
| Data | `data/io.py`, `data/manifest.py`, `data/freshness.py`, `data/catalog.py`, `data/validation.py` | Ingesta OHLCV, manifest con sha256, frescura, catalogo approved |
| Features | `features/engineering.py` (246 lineas) | RSI/MACD/BB/ATR/EWM + `FeatureConfig` opt-in indicator activation |
| Models | `models/baseline.py:65-330`, `models/signals.py:13-192`, `models/promotion.py:11-71` | Baseline logistico, LightGBM, XGBoost, walk-forward con embargo |
| Signals | `models/signals.py` | `ModelSignal` frozen + ATR exits + `SignalPolicyConfig` |
| Arbitration | `execution/paper_signal_arbitration.py` | Fusion de ModelSignals + LLM proposals + shadow + challenger |
| Approval | `execution/telegram_control.py` (1230 lineas) + `execution/paper_signal_approval.py` (383 lineas) | Inbox -> apply -> plan -> dispatch con allowlist; veto one-way fail-closed |
| Execution | `execution/alpaca_paper.py` (602 lineas) + `execution/paper_execute_session.py` + `execution/live_alpaca.py` | Idempotencia con `client_order_id`, retry + `_lookup_existing_order`, kill-switch |
| Observability | `execution/paper_observability.py` (~35k lineas) + `execution/paper_monitor.py` (~47k lineas) | Ledger JSONL append-only + status CRITICAL/WARN/OK |
| Governance | `execution/autonomy_level.py` (711 lineas) + `execution/autonomy_incident_sync.py` (413 lineas) | Maquina N0-N3 per mercado + degradacion idempotente |

### Tier 0 - Capital real efectivo (6 modulos)

Los unicos modulos que pueden llegar a mover dinero real. Cualquier cambio aqui requiere revision del Arquitecto + spec + diff + tests.

| Modulo | Path | Autoridad |
|--------|------|-----------|
| `AlpacaLiveBroker.submit_order` | `src/trading_ai/execution/live_alpaca.py:114-147` | submit real con `submit_enabled=False` por default |
| `run_live_canary` | `src/trading_ai/execution/live_canary.py:71-392` | Punto de invocacion real mas externo; consulta gates A6 |
| `run-live-canary.sh` (wrapper) | `scripts/run-live-canary.sh:10-71` | Triple confirmacion textual exacta |
| `load_risk_config(allow_live=...)` | `src/trading_ai/config.py:71-145` | Audita cada bypass en `reports/tmp/live_bypass_audit.jsonl` |
| `live_execute_session` | `src/trading_ai/execution/live_execute_session.py` (7028 bytes) | Real-submit-ready, NO ejecutado (S12 reclasificado) |
| `LiveCircuitBreaker` | `src/trading_ai/execution/live_circuit_breaker.py` (4488 bytes) | Latch persistente que debe trip-ear al primer signo |

### Tier 1 - Capital paper (autoridad efectiva hoy) (6 modulos)

| Modulo | Path | Autoridad |
|--------|------|-----------|
| `run_paper_execute_session` | `src/trading_ai/execution/paper_execute_session.py:69-510` | Unico que llama `broker.submit_order` real paper |
| `AlpacaPaperBroker.submit_order` | `src/trading_ai/execution/alpaca_paper.py:284-329` | Idempotencia con `client_order_id`, retry con backoff |
| `run_paper_auto_cycle` | `src/trading_ai/execution/paper_auto_cycle.py:58-625` | Operador N0 con candado filesystem (flock) |
| `run_paper_daily` | `src/trading_ai/execution/paper_daily.py:50-1598` | Doble path: offline dry-run o desde readiness broker-confirmed |
| `telegram_control` (4 entry) | `src/trading_ai/execution/telegram_control.py:72-1226` | Inbox/apply/plan/dispatch con allowlist por chat_id |
| `paper_daily_prepare` | `src/trading_ai/evaluation/paper_daily_prepare.py` | Pre-flight que arma el dataset |

### Tier 2 - Gobernanza (autoridad sobre ascenso/descenso) (4 modulos)

| Modulo | Path | Autoridad |
|--------|------|-----------|
| `AutonomyLevel` (N0-N3) | `src/trading_ai/execution/autonomy_level.py` (711 lineas) | Maquina de estados per mercado + ledger append-only |
| `AutonomyIncidentSync` | `src/trading_ai/execution/autonomy_incident_sync.py` (413 lineas) | Degradacion idempotente via `processed_identities` |
| `PaperSignalApproval` | `src/trading_ai/execution/paper_signal_approval.py` (383 lineas) | Veto one-way fail-closed |
| `PaperSwingDeclarations` | `src/trading_ai/execution/paper_swing_declarations.py` (310 lineas) | Exencion EOD con thesis + overnight_loss_pct |

### Boundary paper vs live

El boundary esta implementado en 3 niveles: (1) clientes Alpaca separados (`paper=True` / `paper=False`), (2) unica funcion pura `evaluate_risk_state` que exige el flag de autorizacion live en verdadero para modo `live` (`src/trading_ai/risk/policy.py:55-57`), (3) submit real exige triple confirmacion (env + CLI flag + texto exacto). El autonomy_incident_sync une paper y live: si paper mata el kill-switch, autonomy degrada aunque el breaker live este limpio.

## Cobertura de tests

### Cobertura por sprint

| Sprint | Modulo src | Tests directos | Hallazgos de cobertura |
|--------|-----------|----------------|------------------------|
| A1 | `execution/autonomy_level.py` | 30 (`test_autonomy_level`) | N0->N1 promotions, fail-closed, market preconditions, resolvers |
| A2 | `execution/paper_signal_approval.py` | 25 | approve/veto, fail-closed, hashes, registry integrity |
| A3 | `execution/paper_n0_certification.py` | 20 | clean_days, drawdown, blocker accumulation, forbidden safety flags, artifact_hash re-verifiability |
| A4 | `risk/policy.py` + `execution/paper_position_plan.py` + `execution/paper_position_watch.py` + `execution/live_safe_flatten.py` | 11 (`test_position_plan_stops`) + 2 + 4 | breakeven ratchet, `effective_stop_price`, ATR multipliers |
| A5 | `execution/autonomy_incident_sync.py` | 10 | breaker/reconciliation/kill-switch id sync, idempotencia, sync_state_fail_closed |
| A6 | `execution/live_canary.py` | 32 (3 clases, incluye `LiveCanaryAutonomyGateTests`) | evaluate_autonomy_gate (N1 equities + approved plan) y evaluate_signal_approval_gate (vetoed, missing plan, garbage generated_at) |
| B1 | `execution/paper_telegram_status.py` (con `_autonomy_summary`) | 6 (`test_paper_telegram_status.test_autonomy_section_*`) | incluye `test_autonomy_section_certified_with_accumulating_n0_evidence` |
| B2 | `execution/paper_swing_declarations.py` | 20 | thesis/expires_on/pct/hex hash/duplicate/fail-closed |
| C1 | `evaluation/indicator_activation.py` | 26 | baseline vs extended, fail-closed, hash re-verifiability, defense-in-depth tampered feature_config, side candidates |
| C2 | `execution/llm_context_pack.py` + `execution/llm_signal_proposals.py` | 9 + 16 | `test_indicator_snapshot_present_when_features_provided`, citation gate (valid/empty vocab/unverifiable) |

Total: **~217 tests dedicados a sprints**, sincronizados con el codigo fuente (deltas de minutos a horas). Adicionalmente, la suite cubre el resto de los modulos con **931 tests de fondo** que no estan vinculados a un sprint especifico pero cubren el resto de la superficie.

### Gaps explicitos

Seis modulos de `src/trading_ai/` tienen **cero referencias en tests** (`ref_count=0`). Son candidatos a eliminar o a dotar de cobertura minima:

| Modulo | Path | Riesgo |
|--------|------|--------|
| `paper_safety` | `execution/paper_safety.py` | bajo (agregador, sin autoridad directa) |
| `paper_shadow_outcome` | `execution/paper_shadow_outcome.py` | medio (decisiones shadow sin auditar) |
| `paper_shadow_scorecard` | `execution/paper_shadow_scorecard.py` | medio (sin verificacion de scorecard) |
| `paper_challenger_signals` | `execution/paper_challenger_signals.py` | medio (senales challenger sin cobertura) |
| `mlflow_adapter` | `mlflow_adapter.py` | bajo (adaptador, depende de mlflow extra) |
| `cli_paper` | `cli_paper.py` | bajo (CLI paralelo, no es entry oficial) |

**FR-7** documenta este hallazgo con recomendacion accionable.

### `.coverage` vencido (FR-8)

El archivo `.coverage` en la raiz data del 2026-07-06 10:18, **antes de los commits C1 (`d306d61`) y C2 (`ff13b21`) del 2026-07-07**. Esto significa que la cobertura agregada del 86% reportada en `audit-2026-06-30-mixta.md` no incluye los modulos nuevos de indicadores. `verify-release-minimal.sh` NO corre el gate de cobertura (solo `verify-release.sh` lo hace). Para tener cobertura al dia del estado actual, hay que correr `coverage run --source=src/trading_ai -m unittest discover -s tests` despues de esta auditoria.

## Consistencia idiomatica y patrones de gobierno

Verifique los tres modulos clave de gobernanza y confirme que cumplen el canon descrito en la memoria del repo: `integrity_sha256` + dataclass frozen + atomic write (tempfile + os.replace) + fail-closed + safety block + exit_code estandar.

### Verificacion por modulo

| Aspecto | `paper_risk_state.py` | `autonomy_level.py` | `paper_signal_approval.py` |
|---------|-----------------------|---------------------|---------------------------|
| `integrity_sha256` field | Si (linea 30) | Si (linea 70) | Si (linea 50) |
| Dataclass frozen | Si (`RiskState`, `OrderRiskInputs`) | Si (`AutonomyState`, `AutonomyDecision`) | Si (`ApprovalDecision`) |
| Ledger append-only | No aplica (estado, no ledger) | Si (`_append_ledger_event` 666-696, `ledger.jsonl`) | No aplica (registry append) |
| Atomic write (tempfile + os.replace) | Si (lineas 134-142) | Si (lineas 185-194) | Si (lineas 297-305) |
| Lectura fail-closed | Si (lineas 109-124: cualquier fallo -> kill-switch latched con motivo) | Si (lineas 139-172: missing/corrupt/integrity/unknown -> N0 con `fail_closed=True`) | Si (lineas 91-108: missing/corrupt/integrity/records-not-list -> empty registry con `fail_closed=True`) |
| Idiom `_utc_now` ISO UTC | Si | Si | Si |

**Conclusion**: los tres modulos siguen el mismo patron canonico. NO hay divergencias funcionales. La unica asimetria es que `paper_risk_state` no tiene ledger (es estado plano, no eventos), lo cual es semanticamente correcto.

### Modulos report-only con dataclass frozen + exit_code + payload

Verifique 9+ modulos que cumplen el patron "report-only" definido en la memoria:

| Modulo | Dataclass result | exit_code |
|--------|------------------|-----------|
| `paper_n0_certification.py` | `N0CertificationResult` | Si |
| `autonomy_incident_sync.py` | `IncidentSyncResult` | Si |
| `autonomy_level.py` | `AutonomyDecision` | Si |
| `paper_swing_declarations.py` | `SwingDeclarationDecision` | Si |
| `paper_signal_approval.py` | `ApprovalDecision` | Si |
| `live_canary.py` | `LiveCanaryResult` (+ markdown_path) | Si |
| `paper_telegram_status.py` | `PaperTelegramStatusResult` | Si |
| `llm_context_pack.py` | trio estandar | Si |
| `indicator_activation.py` | `ActivationResult` | Si |
| `live_readiness.py` | `LiveReadinessResult` (con `state` en vez de `status`) | Si (asimetria intencional) |

### Duplicacion sistematica de helpers (FR-5)

Econtre **~39 copias de helpers identicos** distribuidos entre los modulos de gobierno. Todos byte-a-byte identicos, sin divergencia funcional. La consolidacion en `paper_common.py` es deuda tecnica mecanica de bajo riesgo y bajo costo.

| Helper | Copias | Modulos |
|--------|--------|---------|
| `_checksum(body)` | 6 | `autonomy_incident_sync`, `autonomy_level`, `paper_risk_state`, `paper_signal_approval`, `live_circuit_breaker`, `paper_swing_declarations` |
| `_safety()` (dict estandar) | 5+1 | `paper_signal_approval:369`, `paper_swing_declarations:298`, `paper_n0_certification:437`, `indicator_activation:437`, `autonomy_level._decision_payload:638`, `paper_telegram_status:~60` |
| `_is_hex()` | 4 | varios modulos de governance |
| `_dedupe()` | 14 | `_dedupe_blockers`, `_dedupe_issues`, `_dedupe_reasons`, `_dedupe_strings`, etc. |
| `_utc_now()` | 10+ | devuelve `datetime.now(UTC).isoformat()` |
| Atomic write (tempfile + os.replace) | patron replicado en cada modulo de governance |

**Riesgo de NO consolidar**: si el algoritmo de checksum cambia (p.ej. añadir `usedforsecurity=False` para FIPS, o cambiar a SHA3-256), hay que sincronizar manualmente 6 sitios. Los tests son la unica red. Ver FR-5 en la seccion de recomendaciones.

### Asimetria intencional `paper_only` (FR-6)

`live_readiness.py:281-289` define `safety.paper_only: False` mientras que **todos los demas modulos** (`autonomy_level`, `paper_signal_approval`, `paper_n0_certification`, `indicator_activation`, `autonomy_incident_sync`, `paper_swing_declarations`) declaran `paper_only: True`. Esto es **intencional y documentado** (live_readiness contempla el caso canary), pero el nombre del campo confunde: `paper_only: False` se lee como "no es paper-only", cuando significa "ready to consider live canary". Recomendacion FR-6.

## Seguridad

### Scanner textual (PASS)

| Comando | Resultado | Detalle |
|---------|-----------|---------|
| `verify-safety-patterns.py --mode live` | exit 0 | 0 matches de `live_trading_(authorized|allowed)["']?[:=][ \t]*true` en `src/`, `configs/`, `scripts/`, `docs/`, `README.md`, `.github/` |
| `verify-safety-patterns.py --mode futures` | exit 0 | 0 matches de `(futures\|forex)-(execute\|submit)` parser en `src/`, `tests/`; 0 matches de `futures_enabled=True` hardcodeado |
| `grep -rE 'subparsers.add_parser("(futures\|forex)-(execute\|submit)")' src/` | exit 0 | 0 hits |
| `grep -rE 'futures_enabled\s*=\s*True' src/` | exit 0 | 0 hits |

### Secretos en codigo (PASS)

Busqueda exhaustiva en `src/`: 0 secretos hardcoded. Los unicos regex que matchean firmas de secretos estan en `src/trading_ai/execution/paper_common.py:111-120` como redactores pasivos:

- `r"\bnvapi-[A-Za-z0-9_-]+"` (Nvidia NIM API)
- `r"\bgithub_pat_[A-Za-z0-9_-]{20,255}"` (GitHub PAT)

Estas son **firmas para detectar/redactar**, no los secretos en si. Correcto.

### Credenciales reales en `.env` raiz (FR-1) - HALLAZGO CRITICO

**No transcribo los valores reales por politica de esta auditoria.** Confirmo lo siguiente mediante comandos read-only:

```bash
$ stat -c '%a %s %y' .env
600 212 2026-07-07 09:10

$ git check-ignore -v .env
.gitignore:2:.env	.env

$ head -c 60 .env | sed 's/=.*/=REDACTED/'
ALPACA_PAPER_API_KEY=REDACTED
ALPACA_PAPER_SECRET_KEY=REDACTED
TELEGRAM_BOT_TOKEN=REDACTED
TELEGRAM_CHAT_ID=REDACTED
```

**Hallazgo**: existen 4 valores reales (no placeholders) en `.env` en la raiz del repo. El archivo esta excluido por `.gitignore:2` y tiene permisos 600, pero constituye vector de riesgo por:

1. **Shell history**: si el operador ejecuto `source .env` o `cat .env`, los valores pueden quedar en `~/.bash_history` o `~/.zsh_history`.
2. **Git add -f accidental**: si se ejecuta `git add -f .env`, se commitea con secretos. Verifique `git log --all --full-history -- .env` y la salida es vacia (bien).
3. **Logs de error**: si una excepcion captura el entorno en su repr, los valores pueden terminar en `reports/tmp/*.json`.
4. **Contradice el spirit del runbook**: `docs/n0-campaign-runbook.md:11-25` declara explicitamente que las credenciales van en `~/.config/trading-ai/paper.env`, **nunca en archivos del repo**.

**Recomendacion FR-1**: Mover a `~/.config/trading-ai/paper.env` (canonico ya documentado), rotar las claves via Alpaca UI + botfather de Telegram, limpiar shell history, anadir pre-commit hook que detecte `.env` accidental.

### Persistencia de credenciales en el repo

Verifique que NO se persisten secretos en disco dentro del repo:

- `secrets/` no existe.
- `data/` solo contiene 40 bytes (estructura vacia).
- `models/` solo 44 bytes (estructura vacia).
- `configs/permissions.yml` no incluye secretos.
- `reports/tmp/live_bypass_audit.jsonl` audita cada `allow_live=True` cargado desde config (positivo, no contiene secretos).

El archivo `paper_common.py:_SECRET_KEYS` (lineas 29-36) declara el conjunto de variables sensibles. `redact_secrets()` (lineas 101-121) aplica regex de redaccion cuando un artefacto las captura por error.

## Documentacion y drift

### Inventario de `docs/` (24 archivos)

Documentacion principal (13 archivos en raiz de `docs/`):
- `live-transition-sprints.md` (36941 bytes) - plan maestro S0-S13 con matriz de seguridad
- `autonomy-ladder.md` (8760 bytes) - documento rector de la escalera N0-N3
- `paper-real-runbook.md` (70209 bytes, 1536 lineas) - megarrunbook legacy
- `trading-bot-systems-guide.md` (19526 bytes)
- `trading-bot-ai-research.md` (61962 bytes, 1027 lineas) - plan AI-research original
- `threat-model-2026-06-24-algoritmic-ia.md` (20087 bytes) + `threat-model-2026-06-26-prelive.md` (4018 bytes)
- `audit-2026-06-25-offline.md` (5704), `audit-2026-06-26-prelive-complete.md` (8760), `audit-2026-06-30-mixta.md` (8690), `audit-2026-07-06-autonomy-ladder.md` (3570)
- `evidence-2026-07-07-indicator-activation.md` (2598)
- `paper-quickstart.md` (3893)
- `n0-campaign-runbook.md` (5185) - runbook operativo N0

Mas 4 subplanes historicos en `docs/superpowers/plans/` y 6 docs tematicos (`codex-capabilities`, `futures-micro-platform-decision`, `hardware-model-sizing`, `model-evaluation-policy`, `third-party-skills-plugins`, `tooling-risk-register`).

### Drift docs ↔ CLI (DOC-1)

| Sub-comando CLI | Documentado en README | Documentado en autonomy-ladder | Documentado en runbook |
|-----------------|----------------------|-------------------------------|------------------------|
| `paper-trial-day` | NO | NO | NO |
| `autonomy-resolve-incident` | NO | NO | SI (audit-2026-07-06) |
| `autonomy-incident-sync` | NO | NO | SI (B1 audit) |
| `paper-strategy-quality` | SI | NO | NO |
| `adaptive-training-cycle` | SI | NO | NO |
| `model-challenger-report` | SI | NO | NO |

**DOC-1**: el README tiene drift menor con 3 sub-comandos CLI no documentados. Adicionalmente, `paper-challenger-shadow-plan` y `paper-autopilot-plan` SI estan en README pero NO en `n0-campaign-runbook.md`.

### Drift entre runbooks (FR-9)

Tres runbooks coexisten con secciones solapadas y, en algunos puntos, contradictorias:

| Runbook | Tamano | Proposito |
|---------|--------|-----------|
| `paper-quickstart.md` | 3893 bytes | Instalacion + smoke offline |
| `paper-real-runbook.md` | 70209 bytes (1536 lineas) | Megarrunbook legacy con 15 secciones + checklist diario |
| `n0-campaign-runbook.md` | 5185 bytes | Runbook operativo N0 (cron, prerrequisitos, control Telegram) |

**Riesgo**: un operador puede seguir el legacy sin pasar por la escalera N0. El `paper-real-runbook.md` no menciona `autonomy-certify` ni el wrapper `run-paper-auto-cycle.sh`. Adicionalmente, `paper-quickstart.md` seccion 5 menciona `--sessions-root reports/tmp/paper_session` mientras que el default actual es `reports/tmp/paper_monitor/latest.json`.

**FR-9**: consolidar runbooks. El `paper-real-runbook.md` (1536 lineas, junio) deberia marcarse como `superseded by n0-campaign-runbook.md + autonomy-ladder.md` o reorganizarse en modulos tematicos.

### Cobertura de docstrings en `cli.py` (FR-10)

`src/trading_ai/cli.py` (4197 lineas, 212 funciones top-level publicas): **120/212 sin docstring (~57%)**. Ejemplos de funciones criticas sin docstring:

- `run_ai_event_extract`, `run_ai_feature_build`, `run_momentum_vol_target_backtest`
- `add_paper_subcommands`
- `load_risk_config`, `load_universe_config`
- `evaluate_ohlcv_freshness`, `read_records`, `write_records`, `build_dataset_manifest`
- `run_adaptive_training_cycle`, `run_ai_feature_attribution_report`, `run_forecasting_challenger_report`, `run_model_challenger_report`
- `run_cross_asset_session_plan`, `run_futures_readiness_report`, `run_futures_research_scaffold`
- `run_autonomy_incident_sync`
- `build_alpaca_paper_client`, `evaluate_paper_preflight`

**FR-10**: el codigo documenta la intencion en README/runbook pero no en si mismo. Los entry points publicos deberian tener docstring aunque sea de una linea.

### CHANGELOG / ADR / RFC (ausentes)

- `CHANGELOG*`, `CHANGES*`, `RELEASE*`: no encontrado.
- `docs/adr/`: no existe.
- `RFC*`: no encontrado.

Las decisiones arquitectonicas se distribuyen entre `live-transition-sprints.md` (matriz de seguridad), `futures-micro-platform-decision.md` (1 ADR-like) y `threat-model-*.md` (defensivas).

## Estado operativo N0

La campana N0 esta instalada como timer systemd de usuario desde el 2026-07-06, pero **bloqueada** desde el primer intento. Verifique el estado via comandos read-only.

### Timer systemd (activo)

```
● trading-n0.timer - Dias habiles 16:45 (America/Bogota) - campania N0 Algoritmic-IA
     Loaded: loaded (/home/adquiod/.config/systemd/user/trading-n0.timer; enabled; preset: enabled)
     Active: active (waiting) since Mon 2026-07-06 21:58:56 -05; 11h ago
 Invocation: a1d5fec17a764c76954199d6f5ae448f
    Trigger: Tue 2026-07-07 16:45:00 -05; 7h left
   Triggers: ● trading-n0.service
```

Definido en `/home/adquiod/.config/systemd/user/trading-n0.timer` con `OnCalendar=Mon..Fri 16:45` y `Persistent=true`. El servicio `trading-n0.service` ejecuta `ExecStart=/home/adquiod/.config/trading-ai/run-n0-campaign.sh` con `StandardOutput=append:/home/adquiod/Documentos/Algoritmic-IA/reports/tmp/cron_paper_auto.log`.

### Credenciales (`paper.env`) - OPS-1

```
-rw------- 1 adquiod adquiod 438 jul 7 09:12 /home/adquiod/.config/trading-ai/paper.env
```

El archivo existe con permisos 600 correctos. Pero segun el log del cron (ver siguiente seccion), **los valores en el archivo son placeholders o invalidos**: el launcher chequea especificamente `~/.config/trading-ai/paper.env` y falla cerrado si los valores no son autenticos contra la API de Alpaca.

### CSV fresco (FR-2) - AUSENTE

```
$ ls -la data/incoming/fresh_source.csv
ls: cannot access 'data/incoming/fresh_source.csv': No such file or directory

$ ls -la data/raw/approved/core_etfs/1d/
total N
... (Parquet + manifest + catalog del 2026-06-23, 14 dias al 2026-07-07, >5d no califica)
```

**FR-2**: el directorio `data/incoming/` esta vacio. El Parquet aprobado legacy en `data/raw/approved/core_etfs/1d` data del 2026-06-23 (>5 dias). El runbook exige `<5d` para data fresca. La descarga del CSV aprobado es **manual** (gob ernanza explicita: el ingest del repo es sintetico, NO sirve de evidencia).

### Log del cron (1 linea)

```
$ cat reports/tmp/cron_paper_auto.log
[2026-07-07] BLOCKED: credenciales Alpaca paper ausentes (rellena /home/adquiod/.config/trading-ai/paper.env)
```

**OPS-1**: el cron del 2026-07-07 16:45 aun no ha disparado al momento de inspeccion. Solo hay un intento registrado (preparacion al instalar el timer el 2026-07-06 21:58). La salida `BLOCKED` confirma que el fail-closed funciona correctamente (no intento ejecutar con credenciales invalidas), pero el operador debe actuar antes del proximo trigger (Tue 2026-07-07 16:45).

## Proceso y workflow

### Artefactos de proceso (PRC-1)

| Artefacto | Presente | Path | Notas |
|-----------|----------|------|-------|
| CI workflow | SI | `.github/workflows/paper-gates.yml` | Corre `verify-release.sh` + scanner + git diff model |
| CHANGELOG | NO | -- | release notes distribuidas en commits |
| ADR (architecture decision records) | NO | -- | decisiones en `live-transition-sprints.md` + `futures-micro-platform-decision.md` |
| CODEOWNERS | NO | -- | sin asignacion automatica de reviewers |
| Plantilla PR | NO | -- | sin `PULL_REQUEST_TEMPLATE*` |
| Plantilla issue | NO | -- | sin `ISSUE_TEMPLATE*` |
| Plantilla commit | NO | -- | trailers documentados solo en commits |
| Pre-commit hooks | NO | -- | solo `.git/hooks/*.sample` por defecto |
| GitHub Releases | NO | -- | sin tags ni releases |
| Branches activas | 6 remotas | `master`, `live-transition-sprints-impl`, `codex/*` (5) | + worktree `model-intraday-economic-ranking` |

**PRC-1**: el proyecto tiene CI pero carece de plantillas, CODEOWNERS, CHANGELOG, ADR. Cada sprint nuevo depende de que el Arquitecto recuerde el formato (trailers `Co-Authored-By: Claude Fable 5` + frase "Spec by Fable (Architect); implementation by Sonnet (Executor); architect review approved").

### Trailers de commits (FR-4)

Verifique los ultimos 18 commits sobre `live-transition-sprints-impl`:

```
2858fbb Complete indicator activation measurement with all model classes
73246d1 Make indicator activation comparison sensitive and record real-data result
d306d61 Add evidence-gated activation of extended technical indicators (C1)
ff13b21 Add indicator snapshot to LLM context and citation gate to proposals (C2)
fbad208 Record installed N0 campaign scheduling (systemd user timer)
e141f69 Update sprint board with B1/B2 and deferred N2 runner note
dc25188 Add swing-position declarations with mandatory overnight risk plan (Sprint B2)
d704e2b Fix N0 runbook cron line after end-to-end smoke test
8106a93 Add N0 evidence campaign runbook
0f4d074 Surface autonomy ladder in Telegram status and daily cycle (Sprint B1)
... (8 mas: c97b3cd A6, 93bccaf A5, ae5656c A4, 38403c3 A3, e47ff50 A2, 5cb84ed A1, e141f69 sprint board, d456165 baseline)
```

Cada uno contiene:
- `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Lineas como "Spec by Fable (Architect); implementation by Sonnet (Executor); architect review approved."
- `Gates: <N> unittest OK, live/futures/forex safety scans clean, git diff --check clean.`

**FR-4 (HALLAZGO)**: el trailer `Co-Authored-By` aparece en uno solo de los dos modelos (Fable). El rol del usuario declara que **Fable=Arquitecto (revisa) + Sonnet=Ejecutor (implementa)**, pero los commits no reflejan al Ejecutor explicitamente. Esto crea dos problemas:

1. **Trazabilidad incompleta**: si una implementacion tiene un bug, no se puede atribuir a quien escribio el codigo vs quien lo aprobo.
2. **Inconsistencia con la convencion declarada**: el runbook interno y la memoria describen dos roles diferenciados; el trailer los colapsa en uno.

**Recomendacion FR-4**: aniadir `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` en commits donde la implementacion sea del Ejecutor, o documentar explicitamente en `docs/autonomy-ladder.md` que el trailer refleja solo al firmante del veredicto de revision.

### Estado de git (FR-3)

```
$ git status -sb
# rama local: live-transition-sprints-impl
# divergencia: [adelante 18] sobre origin

$ git tag -l
(vacio)

$ git stash list
(vacio)
```

**FR-3**: 18 commits sin pushear sobre `origin/live-transition-sprints-impl`. Esto significa que la rama esta divergente del remoto y el CI workflow (que dispara en push a `master` y `codex/**`) **no valida automaticamente los sprints nuevos**. Ademas, los 18 commits representan la totalidad del trabajo A1-A6 + B1-B2 + C1-C2 que el Arquitecto aprobo pero que el remoto aun no ha visto. Si el repositorio se perdiera localmente, todo este trabajo se perderia.

## Hallazgos priorizados

13 hallazgos numerados con anclas para cross-referencia entre secciones y para que la proxima auditoria pueda decir "FR-X cerrado en commit Y".

| # | Severidad | Ancla | Titulo | Seccion |
|---|-----------|-------|--------|---------|
| 1 | CRITICO | FR-1 | Credenciales reales en `.env` raiz (riesgo commit accidental, shell history, log filter) | 8 |
| 2 | CRITICO | FR-2 | CSV fresco ausente (`data/incoming/fresh_source.csv`); aprobado legacy de 2026-06-23 (>5d) | 10 |
| 3 | CRITICO | FR-3 | 18 commits sin pushear sobre `origin/live-transition-sprints-impl` | 11 |
| 4 | CRITICO | FR-4 | Solo Fable firma trailers, el rol Arquitecto+Ejecutor no se refleja en los commits | 11 |
| 5 | IMPORTANTE | FR-5 | Duplicacion sistematica de helpers (`_checksum` x6, `_safety` x5, `_is_hex` x4, `_dedupe` x14, `_utc_now` x10+) | 7 |
| 6 | IMPORTANTE | FR-6 | Asimetria intencional pero confusa: `live_readiness.py:281-289` tiene `safety.paper_only: False` mientras los demas tienen True | 7 |
| 7 | IMPORTANTE | FR-7 | 6 modulos con `ref_count=0` en tests (`paper_safety`, `paper_shadow_outcome`, `paper_shadow_scorecard`, `paper_challenger_signals`, `mlflow_adapter`, `cli_paper`) | 6 |
| 8 | IMPORTANTE | FR-8 | `.coverage` vencido (2026-07-06 10:18, pre-C1/C2) | 6 |
| 9 | IMPORTANTE | FR-9 | Drift runbooks: `paper-quickstart.md` vs `paper-real-runbook.md` vs `n0-campaign-runbook.md` con solapamiento y contradicciones | 9 |
| 10 | OPERATIVO | FR-10 | Cobertura de docstrings en `cli.py`: 120/212 funciones publicas sin docstring (~57%) | 9 |
| 11 | OPERATIVO | OPS-1 | N0 cron `BLOCKED: credenciales Alpaca paper ausentes` (unica linea, primer intento fallido) | 10 |
| 12 | OPERATIVO | OPS-2 | Sin CODEOWNERS, sin plantillas PR/issue, sin CHANGELOG/ADR/RFC | 11 |
| 13 | OPERATIVO | OPS-3 | El `.env` contradice el spirit del runbook ("nunca en archivos del repo") aunque esta gitignored | 8 |

## Recomendaciones accionables

Cada hallazgo tiene una recomendacion concreta con esfuerzo estimado (S = <1 sprint, M = 1-2 sprints, L = >2 sprints), riesgo-si-no y owner sugerido.

### Tabla densa de recomendaciones

| # | Ancla | Hallazgo | Accion concreta | Esfuerzo | Riesgo-si-no | Owner |
|---|-------|----------|-----------------|----------|--------------|-------|
| R-1 | FR-1 | Credenciales reales en `.env` raiz | (a) Mover a `~/.config/trading-ai/paper.env` con valores correctos. (b) Rotar claves via Alpaca UI + botfather. (c) Limpiar `~/.bash_history` y `~/.zsh_history`. (d) Anadir pre-commit hook que detecte `.env`. (e) Confirmar `git log --all --full-history -- .env` permanece vacio. | S (1 PR, ~30 min) | medio-bajo degradable a alto por error humano | Operador (con supervision del Arquitecto) |
| R-2 | FR-2 | CSV fresco ausente | Descargar CSV aprobado del proveedor, colocar en `data/incoming/fresh_source.csv` con fecha <5 dias, ejecutar `validate_ohlcv` antes del cron. | S (descarga manual, ~1h) | cron sigue bloqueado, campana N0 no arranca | Operador |
| R-3 | FR-3 | 18 commits sin pushear | Push de `live-transition-sprints-impl` a `origin` despues de revision final del Arquitecto sobre los 18 commits. | S (1 comando, ~5 min) | trabajo perdido si el local se corrompe, CI no valida sprints nuevos | Arquitecto |
| R-4 | FR-4 | Solo Fable firma trailers | Documentar en `docs/autonomy-ladder.md` la convencion de trailers. Considerar anadir `Co-Authored-By: Claude Sonnet 5` en commits donde Sonnet escribio codigo bajo spec de Fable. | S (1 doc PR, ~15 min) | trazabilidad incompleta entre implementacion y revision | Arquitecto |
| R-5 | FR-5 | Duplicacion de helpers | Consolidar `_checksum`, `_safety`, `_is_hex`, `_dedupe`, `_utc_now` y atomic-write en `paper_common.py`. Migrar las 6+5+4+14+10+ ~39 copias. Tests existentes deben seguir verdes sin modificacion. | M (1 sprint, refactor cross-modulo con verificacion de 1148 tests) | drift silencioso si algoritmo de checksum cambia, sync manual | Ejecutor (bajo spec del Arquitecto) |
| R-6 | FR-6 | Asimetria `paper_only` | Renombrar campo en `live_readiness.py:281-289` a `ready_for_live_canary: bool` separado de `paper_only`, o documentar explicitamente la semantica. | S (1 linea de codigo + doc) | confusion para futuros lectores | Ejecutor |
| R-7 | FR-7 | 6 modulos sin tests | Anadir tests minimos a `paper_safety`, `paper_shadow_outcome`, `paper_shadow_scorecard`, `paper_challenger_signals`. Para `mlflow_adapter` y `cli_paper`, decidir: o se usan (anadir tests) o se eliminan. | M (1 sprint, depende de decisions de diseno) | superficie sin cobertura que puede regresar silenciosamente | Arquitecto (decision) + Ejecutor (implementacion) |
| R-8 | FR-8 | `.coverage` vencido | Correr `coverage run --source=src/trading_ai -m unittest discover -s tests -q` y commitear `.coverage` refreshed. Verificar que el porcentaje sigue >= 75%. | S (5 min) | el 86% reportado en `audit-2026-06-30-mixta.md` no refleja C1/C2 | Operador |
| R-9 | FR-9 | Drift runbooks | Marcar `paper-real-runbook.md` como `superseded by n0-campaign-runbook.md + autonomy-ladder.md`. Mover secciones utiles de `paper-quickstart.md` a `n0-campaign-runbook.md`. | M (1 sprint editorial) | operador sigue el legacy sin pasar por la escalera N0 | Arquitecto |
| R-10 | FR-10 | Docstrings faltantes en `cli.py` | Anadir docstrings de una linea a las 120 funciones publicas restantes en `cli.py`. Priorizar las que tienen riesgo de uso incorrecto (`load_risk_config`, `build_alpaca_paper_client`, `evaluate_paper_preflight`). | M (1 sprint mecanico) | entry points documentados solo en README, no en codigo | Ejecutor |
| R-11 | OPS-1 | N0 cron bloqueado | Resolver FR-1 + FR-2; el cron se desbloquea automaticamente. Validar primer trigger manual con `systemctl --user start trading-n0.service` y revisar `reports/tmp/cron_paper_auto.log`. | S (depende de R-1 + R-2) | campana N0 no acumula evidencia, N1 nunca certificado | Operador |
| R-12 | OPS-2 | Falta artefactos de proceso | Crear `CODEOWNERS` con `@dandrespd` para `src/trading_ai/execution/` + `scripts/`. Crear plantillas `.github/PULL_REQUEST_TEMPLATE.md` + `.github/ISSUE_TEMPLATE/bug_report.md` + `.github/ISSUE_TEMPLATE/feature_request.md`. Considerar CHANGELOG.md y `docs/adr/` para decisiones futuras. | M (1 sprint, ~4-6 PRs pequenos) | cada sprint depende de memoria institucional del Arquitecto | Arquitecto |
| R-13 | OPS-3 | `.env` contradice spirit del runbook | Anadir seccion explicita en `n0-campaign-runbook.md` y `paper-quickstart.md` sobre "Por que NO debe haber un `.env` en el repo aunque este gitignored" con la justificacion de seguridad operacional. | S (2 parrafos en docs) | futuros operadores repiten el patron | Arquitecto |

### Esta semana (4 items criticos)

1. **R-1**: rotar y mover credenciales (Operador).
2. **R-2**: descargar y validar CSV fresco (Operador).
3. **R-3**: push de los 18 commits (Arquitecto, con revision final).
4. **R-11**: validar primer trigger manual del cron (Operador, depende de R-1 + R-2).

### Proximo sprint (5 items importantes)

5. **R-5**: consolidar helpers en `paper_common.py` (Ejecutor bajo spec).
6. **R-8**: refrescar `.coverage` (Operador).
7. **R-6**: renombrar `paper_only` en `live_readiness.py` (Ejecutor).
8. **R-9**: marcar `paper-real-runbook.md` como superseded (Arquitecto).
9. **R-4**: documentar convencion de trailers en `autonomy-ladder.md` (Arquitecto).

### Despues del proximo sprint (4 items operativos)

10. **R-7**: anadir tests a modulos `ref_count=0` (Arquitecto + Ejecutor).
11. **R-10**: docstrings en `cli.py` (Ejecutor).
12. **R-12**: CODEOWNERS + plantillas + CHANGELOG + ADR (Arquitecto).
13. **R-13**: documentar "no `.env` en el repo" en runbooks (Arquitecto).

## Riesgos residuales

Tras cerrar los 13 hallazgos, quedan riesgos abiertos que se conviven y se monitorizan, no que se cierran en esta auditoria.

- **Riesgo humano del SPOF del Arquitecto**: si Fable no esta disponible, ningun sprint puede cerrar. El proyecto depende de una sola persona para validar gates de N1+. Mitigacion parcial: el modelo Sonnet puede operar autonomamente en tareas de implementacion bajo spec.
- **Riesgo humano del SPOF de `dandrespd`**: el reviewer declarado en el runbook es unico. Si se ausenta o cambia, no hay redundancia.
- **Riesgo humano del CSV manual**: el operador debe descargar y validar el CSV aprobado antes de cada campana. Si se va de vacaciones o se olvida, la campana se detiene.
- **Riesgo del drift de `.coverage`**: cada vez que se ejecuta `verify-release.sh` se regenera `.coverage`, pero la falta de automatizacion del refresh despues de cada merge significa que el snapshot en disco siempre tiene desfase.
- **Riesgo del modelo champion no versionado**: `models/latest_model.json` se mantiene por git diff check, pero el modelo actual de produccion puede divergir del "latest" si el operador entrena offline y no commitea.
- **Riesgo de los modulos `ref_count=0`**: aunque sean candidatos a eliminar, no se puede confirmar que no se usan en runtime. La migracion a tests minimos (R-7) es necesaria antes de tomar decision de remocion.
- **Riesgo del `.env` despues de mover**: aunque se mueva a `~/.config/trading-ai/paper.env`, la copia en `.env` raiz persiste hasta que se borre manualmente (gitignored, pero fisicamente presente).

### 15.1 Preguntas abiertas al operador

1. **Credenciales**: confirmar si los valores reales en `.env` son intencionales o accidentales. Si son intencionales, justifique el uso frente al path canonico `~/.config/trading-ai/paper.env`. Si son accidentales, rotar inmediatamente.
2. **Push de 18 commits**: confirmar que el Arquitecto ha validado los 18 commits individualmente (no solo el ciclo A1-A6 + B1-B2 + C1-C2 en abstracto) antes del push a `origin`.
3. **Refactor de `_checksum`/`_safety`/etc.**: aceptar la consolidacion como sprint formal (con spec del Arquitecto) o mantener la duplicacion controlada con un test canon que verifique que todas las copias producen el mismo hash. La primera opcion tiene riesgo de regresion (~13 imports a actualizar); la segunda opcion agrega deuda tecnica pero reduce riesgo inmediato.
4. **Modulos `ref_count=0`**: decidir caso por caso entre (a) anadir tests minimos, (b) eliminar si estan en desuso, (c) documentar como "experimental / sin uso actual". Mi recomendacion es (a) para los que viven en `execution/` (autoridad), (b) para `mlflow_adapter` si no se usa, (c) para `cli_paper` que parece CLI paralelo.
5. **CHANGELOG / ADR**: vale la pena formalizar el changelog y los ADR ahora, o esperar al primer release tag (que tampoco existe)? El proyecto esta cerca de la transicion N0 -> N1, lo cual justifica un release formal con tag.

## Anexo: comandos y outputs clave

Solo los comandos que demuestran evidencia unica. La salida completa esta disponible si se requiere re-verificacion.

### A.1 Verificacion de `.env` (sin secretos transcritos)

```bash
$ git check-ignore -v .env
.gitignore:2:.env	.env

$ stat -c '%a %s %y' .env
600 212 2026-07-07 09:10

$ head -c 60 .env | sed 's/=.*/=REDACTED/'
ALPACA_PAPER_API_KEY=REDACTED
ALPACA_PAPER_SECRET_KEY=REDACTED
TELEGRAM_BOT_TOKEN=REDACTED
TELEGRAM_CHAT_ID=REDACTED

$ git log --all --full-history -- .env
(vacio - nunca se commiteo)
```

### A.2 Estado del timer N0

```bash
$ systemctl --user status trading-n0.timer
● trading-n0.timer - Dias habiles 16:45 (America/Bogota) - campania N0 Algoritmic-IA
     Loaded: loaded (/home/adquiod/.config/systemd/user/trading-n0.timer; enabled; preset: enabled)
     Active: active (waiting) since Mon 2026-07-06 21:58:56 -05; 11h ago
 Invocation: a1d5fec17a764c76954199d6f5ae448f
    Trigger: Tue 2026-07-07 16:45:00 -05; 7h left
   Triggers: ● trading-n0.service
```

### A.3 Log del cron N0

```bash
$ cat reports/tmp/cron_paper_auto.log
[2026-07-07] BLOCKED: credenciales Alpaca paper ausentes (rellena /home/adquiod/.config/trading-ai/paper.env)
```

### A.4 Commits sin pushear (18 sobre origin)

```
2858fbb Complete indicator activation measurement with all model classes
73246d1 Make indicator activation comparison sensitive and record real-data result
d306d61 Add evidence-gated activation of extended technical indicators (C1)
ff13b21 Add indicator snapshot to LLM context and citation gate to proposals (C2)
fbad208 Record installed N0 campaign scheduling (systemd user timer)
e141f69 Update sprint board with B1/B2 and deferred N2 runner note
dc25188 Add swing-position declarations with mandatory overnight risk plan (Sprint B2)
d704e2b Fix N0 runbook cron line after end-to-end smoke test
8106a93 Add N0 evidence campaign runbook
0f4d074 Surface autonomy ladder in Telegram status and daily cycle (Sprint B1)
c97b3cd Add autonomy and signal-approval gates to run-live-canary (Sprint A6)
93bccaf Add autonomy incident sync with idempotent downgrade (Sprint A5)
ae5656c Add breakeven ratchet and effective stop price (Sprint A4)
38403c3 Add paper N0 certification artifact (Sprint A3)
e47ff50 Add paper signal approval registry and Telegram veto intent (Sprint A2)
5cb84ed Add autonomy level state machine with fail-closed (Sprint A1)
d456165 Baseline: Telegram control/notify, forex readiness, plan EOD, IA features, scanner extended
e141f69 Update sprint board with B1/B2 and deferred N2 runner note
```

### A.5 Salida de `verify-release-minimal.sh` (resumida)

```bash
$ ./scripts/verify-release-minimal.sh
[gate 1/7] minimal paper environment -> PASS
[gate 2/7] minimal focused paper tests -> PASS (89/89 tests OK in 2.134s)
[gate 3/7] minimal full unittest suite -> PASS (64/64 tests OK in 1.297s)
[gate 4/7] minimal git diff whitespace -> PASS
[gate 5/7] minimal latest model unchanged -> PASS
[gate 6/7] minimal live authorization safety scan -> PASS
[gate 7/7] minimal futures execution parser scan -> PASS

EXIT_CODE=0
```

### A.6 Salida de los scanners de seguridad

```bash
$ python3 scripts/verify-safety-patterns.py --mode live
EXIT_CODE=0 (0 matches)

$ python3 scripts/verify-safety-patterns.py --mode futures
EXIT_CODE=0 (0 matches)

$ grep -rE 'subparsers.add_parser("(futures|forex)-(execute|submit)")' src/
(0 hits)

$ grep -rE 'futures_enabled\s*=\s*True' src/
(0 hits)
```

---

**Fin de la auditoria.** Decisiones operativas abiertas: las 4 recomendaciones "esta semana" (R-1, R-2, R-3, R-11) son prerequisito para que la campana N0 arranque y se acumule evidencia limpia. El resto del backlog se ejecuta en orden de esfuerzo inverso: primero los S (~30 min), luego los M (~1 sprint), luego los L (no aplica en esta auditoria).

Proxima auditoria sugerida: cuando se acumulen 5 dias habiles limpios de campana N0 (alrededor del 2026-07-14 si se desbloquea hoy), verificar metricas de `paper-n0-certification`, refrescar `.coverage`, validar que los SPOFs humanos estan distribuidos, y emitir `audit-2026-07-14-campana-N0.md` como par de esta.