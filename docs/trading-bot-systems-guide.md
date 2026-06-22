# Guia de contexto: sistemas de trading con bots, estrategias y LLMs

Fecha de investigacion: 2026-06-18  
Uso previsto: contexto general para evolucionar este repo. No autoriza trading
live, no reemplaza asesoria financiera/legal/fiscal y no cambia los gates de
paper trading ya definidos.

## Decision principal

El camino mas robusto para este proyecto no es construir un "LLM trader" que
decida ordenes. La arquitectura recomendada es:

1. Motor determinista para datos, senales, riesgo, ordenes, auditoria y
   reconciliacion.
2. Modelos estadisticos/ML como generadores de senales comparados contra
   baselines.
3. LLM como capa de investigacion, revision, explicacion y auditoria con salida
   estructurada.
4. Controlador operativo que recomienda el siguiente paso, pero no ejecuta
   broker por si solo.
5. Paper broker-confirmed con aprobaciones explicitas antes de pensar en live.

Esto mantiene alineado el repo con los sprints actuales: gobierno de modelo,
`llm-paper-review`, `paper-autopilot-plan`, paper confirmado minimo y futures
research-only.

## Principios no negociables

- El LLM no tiene autoridad de ejecucion: no envia ordenes, no cambia riesgo,
  no lee `.env`, no aprueba live y no decide capital.
- Toda promocion de modelo requiere evidencia out-of-sample, costos, slippage,
  turnover, paper fills reales y decision humana.
- El bot debe ser auditable por artefactos: JSON, Markdown, hashes, rutas,
  comandos, fechas y razones de bloqueo.
- Backtest, paper y live deben compartir la mayor cantidad posible de logica.
  Donde no haya paridad, la diferencia debe quedar documentada.
- Futures se mantiene research-only hasta estabilizar ETFs/paper y completar
  evidencia broker-confirmed.

## Sistemas similares revisados

| Sistema | Que aporta | Riesgo o limite | Leccion para este repo |
| --- | --- | --- | --- |
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean) | Motor event-driven abierto para backtesting y live trading; CLI con `lean backtest`, `lean optimize` y `lean live`. | Complejidad y dependencia de ecosistema/datos/brokers. | Buen modelo de referencia para modularidad, CLI y research-to-live, no hace falta migrar el repo aun. |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Motor Rust/Python multi-activo y multi-venue con simulacion determinista, live execution y paridad research-to-live. | Curva alta; todavia evoluciona y advierte no usar versiones de desarrollo en produccion live. | Excelente referencia para event sourcing, precision, adaptadores y separacion entre core de ejecucion y control plane. |
| [Freqtrade](https://github.com/freqtrade/freqtrade) + [FreqAI](https://www.freqtrade.io/en/stable/freqai/) | Bot crypto con dry-run, backtesting, protecciones, WebUI/Telegram y ML adaptativo con retraining. | Enfocado en crypto; los ejemplos no son para produccion; alto riesgo de overfit si se automatiza tuning sin gates. | Copiar ideas de dry-run, retraining auditado, `lookahead-analysis` y controles, no copiar enfoque crypto como core inicial. |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | Framework para bots en CEX/DEX con conectores REST/WebSocket y foco en market making/venues crypto. | Mas util para crypto/liquidez que para ETFs y futuros regulados. | Referencia para conectores, estrategias modulares y separacion de exchange adapter; no usar como backbone del MVP ETF. |
| [FinGPT](https://arxiv.org/abs/2306.06031) | LLM financiero open source con enfoque data-centric, data curation y LoRA. | Util para NLP financiero; no valida por si mismo ejecucion rentable. | Usarlo como inspiracion para RAG/sentiment/features; no como trader autonomo. |
| [FinRobot](https://arxiv.org/abs/2405.14767) | Plataforma de agentes financieros con capas de agentes, algoritmos LLM, LLMOps/DataOps y modelos fundacionales. | Arquitectura amplia; requiere mucha gobernanza para tareas con side effects. | Buena referencia para dividir agentes por responsabilidad: analyst, reviewer, risk, compliance. |
| [TradingAgents](https://arxiv.org/abs/2412.20138) | Multi-agent framework con analistas tecnico/fundamental/sentiment, debate bull/bear y risk management. | Riesgo de teatralizar decisiones sin evidencia broker-real; resultados dependen del setup. | Usar el patron de "debate + riesgo + revision" solo para investigacion y explicacion, no para ordenes. |
| [LiveTradeBench](https://arxiv.org/abs/2511.03628) | Benchmark live para agentes LLM con precios/noticias y asignacion de portafolio. | Muestra que benchmarks estaticos no prueban competencia secuencial real. | Los LLMs deben evaluarse en entornos con incertidumbre temporal; paper/live evidence vale mas que score general. |
| [QuantCode-Bench](https://arxiv.org/abs/2604.15151) y [BacktestBench](https://arxiv.org/abs/2605.17937) | Evalua generacion de estrategias/backtests por LLMs. | Las fallas principales no son solo sintaxis: tambien logica financiera, API y conducta observable. | Si el LLM genera codigo o estrategias, debe pasar tests, backtests reproducibles y revision humana antes de entrar al pipeline. |
| [TimesFM](https://github.com/google-research/timesfm) y [Chronos](https://github.com/amazon-science/chronos-forecasting) | Modelos fundacionales de forecasting de series temporales, zero-shot/fine-tuning y forecast probabilistico. | Forecast no equivale a alpha; hay que comparar contra baselines simples y costos. | Usarlos como challengers de forecasting, nunca como senal productiva sin champion/challenger y paper. |

## Arquitectura objetivo para el repo

```text
data/raw + providers
  -> data validation + manifests
  -> feature sets versionados
  -> research/backtest
  -> model registry + champion/challenger
  -> signal generation
  -> deterministic risk gate
  -> paper execution adapter
  -> broker reconciliation + performance
  -> evidence index + ops check

LLM layer:
  reads artifacts only
  -> produces review JSON/Markdown
  -> explains blockers and risks
  -> never calls broker or secrets

Autopilot planner:
  reads readiness/ops/review/permissions
  -> emits next operational step
  -> never executes broker
```

La capa LLM debe ser lateral y auditable. No debe vivir dentro del camino critico
que convierte senales en ordenes. El camino critico debe seguir siendo codigo
determinista con permisos explicitos.

## Como construir bots en este contexto

1. Definir mercado y restriccion operativa: ETFs paper primero; futures solo
   investigacion.
2. Construir datos reproducibles: manifest, hash, freshness, split temporal y
   validacion de columnas.
3. Crear baseline determinista: buy/hold, cash/ETF rotation simple, momentum
   de baja frecuencia o mean reversion simple.
4. Entrenar challengers solo contra baseline: LightGBM/XGBoost/sklearn, y
   modelos de series temporales como TimesFM/Chronos si aportan forecast
   medible.
5. Evaluar con walk-forward, costos, slippage, turnover, drawdown, numero de
   trades y sensibilidad por regimen.
6. Pasar por gobierno de modelo: challenger report, decision humana y cycle
   report.
7. Ejecutar paper con broker confirmado solo con aprobaciones explicitas.
8. Reconciliar fills, statements, closeout, performance y ops check.
9. Repetir la rutina hasta tener evidencia estable antes de ampliar mercados.

La regla practica: si una pieza no genera artefacto verificable, no cuenta como
evidencia.

## Estrategias recomendadas por madurez

### Fase 1: robustez de infraestructura

- Long/cash ETF rotation: SPY/QQQ/IWM/TLT/GLD/cash con reglas simples.
- Momentum cross-sectional mensual/semanal en ETFs liquidos.
- Trend following diario con volatilidad objetivo y drawdown stop.
- Mean reversion diaria solo si el costo y turnover quedan controlados.

Objetivo: probar datos, calendario, senales, sizing, performance y auditoria.
No optimizar agresivamente.

### Fase 2: ML supervisado controlado

- Modelos tabulares para ranking/probabilidad de retorno por horizonte.
- Features de precio, volumen, volatilidad, regimen, calendario y macro
  disponible al momento de decision.
- Etiquetas con horizonte explicito y purging/embargo si se solapan.
- Optuna solo con max trials, seed, OOS fijo y registro de todos los intentos.

Objetivo: demostrar mejora sobre baseline despues de costos y con suficientes
trades.

### Fase 3: LLM/NLP como features y auditoria

- RAG de noticias, filings, calendario macro y runbooks.
- Extraccion estructurada de eventos: ticker, fecha, tipo, severidad, fuente,
  incertidumbre y ventana de validez.
- Sentiment solo como feature secundaria y con timestamp estricto.
- Resumen de riesgos y anomalas operativas para revision humana.

Objetivo: mejorar contexto y control, no reemplazar el motor de ordenes.

### Fase 4: futuros research-only

- Micro E-mini como primer universo candidato cuando ETFs/paper este estable.
- Empezar con datos diarios/horarios y modelar roll, margen, session breaks y
  horarios.
- No scalping ni HFT hasta tener datos tick/order book, costos reales y motor
  de ejecucion probado.

Objetivo: investigacion reproducible, no ejecucion.

### Fase 5: RL y agentes avanzados

- Solo simulacion aislada.
- Comparar contra baselines simples.
- Rechazar cualquier agente que no respete limites de posicion, drawdown,
  turnover y costos.
- Nunca promover RL por retorno bruto de backtest.

Objetivo: explorar, no operar.

## Construccion de LLMs y agentes

Para este proyecto, "construir LLMs" debe interpretarse como construir una capa
LLM gobernada, no entrenar un modelo fundacional desde cero.

### Lo recomendado

- Usar modelos locales con licencia permisiva desde `configs/llm_local_models.json`:
  `Qwen/Qwen3-0.6B` para LoRA, `Qwen/Qwen3-1.7B` para inferencia local,
  `ibm-granite/granite-3.3-2b-instruct` como alterno y
  `microsoft/Phi-4-mini-instruct` solo si el hardware lo permite.
- Usar JSON Schema local para que reviews, extracciones y decisiones auditables
  salgan como artefactos validables.
- Cargar pesos solo desde cache local con `local_files_only=True`; si el cache
  falta, bloquear en vez de descargar en runtime.
- Aplicar guardrails: schema estricto, allowlist de archivos, redaccion de
  secretos, prohibicion de broker/live, y `llm_authority="none"`.
- Crear evals locales para el LLM reviewer: casos de readiness READY/BLOCKED,
  ops WARN/CRITICAL, evidencia faltante, secretos simulados y peticiones de
  orden directa.

### Lo que no se recomienda ahora

- Entrenar un LLM desde cero.
- Darle al LLM credenciales, shell generico, broker adapter o permiso de editar
  riesgo.
- Usar OpenAI/API externa para inferencia, supervision o evaluacion del flujo de
  trading.
- Convertir "debate de agentes" en aprobacion de ordenes.
- Usar sentiment como gatillo unico de compra/venta.
- Fine-tuning antes de tener dataset de errores, rubricas y evaluacion estable.

### Cuando considerar fine-tuning o adapters

Solo despues de acumular ejemplos reales de tareas estrechas:

- clasificar blockers operativos;
- extraer eventos financieros con schema estable;
- normalizar reportes broker;
- detectar inconsistencias en evidencia;
- resumir razones de modelo sin inventar metricas.

El criterio de entrada es tener dataset, train/validation/test, grader y metricas
de regresion. Si el prompt + RAG + schema resuelve el problema, no se fine-tunea.

## Gobierno de modelo

El gobierno de modelo debe seguir un flujo champion/challenger:

1. Champion actual queda fijo hasta demostrar mejora.
2. Challenger report compara con datos versionados, costos y OOS.
3. Si faltan fills paper reales, el ciclo queda `DEFERRED` o `BLOCKED`.
4. Decision humana registra `APPROVE`, `DEFER` o `REJECT`.
5. `models/latest_model.json` no se toca sin decision aprobada y evidencia.

Buenas practicas a mantener:

- split temporal cerrado;
- walk-forward cuando hay varios regimenes;
- purging/embargo si las etiquetas miran al futuro;
- registro de todos los trials para evitar cherry-picking;
- DSR/PBO o equivalentes cuando haya busqueda grande de parametros;
- stress tests de datos faltantes, gaps, rechazo de orden y broker offline.

## MLOps minimo

- [MLflow Model Registry](https://mlflow.org/docs/latest/ml/model-registry/)
  como patron para versionar modelos, estados, tags, descripciones y lineage.
- [Optuna](https://optuna.readthedocs.io/en/stable/) solo con limites duros,
  seeds y artefactos por trial.
- Drift/data quality con reportes locales antes de agregar servicios pesados.
- Feature store simple con parquet/manifest primero; Feast o infraestructura
  mayor solo si aparece duplicacion real entre training/inference.

La madurez se mide por reproducibilidad y trazabilidad, no por numero de
herramientas instaladas.

## Controles operativos

| Riesgo | Control requerido |
| --- | --- |
| Lookahead/leakage | timestamps estrictos, split temporal, purging/embargo, tests de leakage. |
| Sobreoptimizacion | max trials, registro completo, OOS bloqueado, comparacion contra naive/baseline. |
| Costos ignorados | comisiones, slippage, spread, turnover y capacidad antes de aprobar. |
| Orden accidental | confirmaciones explicitas, broker paper-only, no side effects en LLM/autopilot. |
| Credenciales | no leer `.env` desde LLM; redaccion de secretos en reportes; permisos minimos. |
| Drift | freshness, schema checks, distribuciones, performance decay y alertas. |
| Broker mismatch | fills, statements, closeout, posiciones y cash reconciliados. |
| Riesgo regulatorio | verificar broker/producto/permisos; NFA BASIC para derivados; no live sin revision separada. |

Para derivados/futuros en EE. UU., [NFA BASIC](https://www.nfa.futures.org/basicnet/)
es una fuente oficial para revisar antecedentes de profesionales/firmas de la
industria de derivados. FINRA tambien publica guias de supervision para firmas
con estrategias algoritmicas, incluyendo controles, pruebas y responsabilidad
operativa, como [Regulatory Notice 15-09](https://www.finra.org/rules-guidance/notices/15-09).

## Matriz de autoridad

| Componente | Puede leer | Puede escribir artefactos | Puede ejecutar broker | Puede aprobar live |
| --- | --- | ---: | ---: | ---: |
| LLM reviewer | reportes y evidencia allowlisted | si, review JSON/MD | no | no |
| Autopilot planner | readiness, ops, reviews, permisos | si, plan JSON/MD | no | no |
| Modelo ML | features versionadas | si, predicciones/senales | no | no |
| Risk gate | senales, posiciones, policy | si, decision de riesgo | no directamente | no |
| Broker adapter paper | orden autorizada y config paper | si, fills/evidence | solo con confirmaciones | no |
| Humano | todo artefacto auditado | decision/review | puede autorizar paper | live fuera de alcance |

## Backlog recomendado

### Ahora

- Mantener ETFs/paper como camino principal.
- Usar `paper-auto-cycle` como operador automatico simple, paper-only y
  cronable.
- Mantener `llm-signal-proposals` en modo shadow/gobernado con
  `llm_authority=none`.
- Usar `paper-signal-arbitration` como frontera determinista entre baseline,
  LLM y orden paper.
- Usar `paper-operator-status` como gate limpio antes de cualquier
  `paper-auto-cycle --confirm-paper-auto --require-clean-state`.
- Mantener `session_ledger.jsonl` de `paper-auto-cycle` como bitacora
  append-only de ciclos limpios y bloqueados.
- Bloquear ciclos confirmados duplicados por `as_of_date` y reportar locks
  activos/stale sin borrarlos automaticamente.
- Dejar modelo governance en `DEFERRED` cuando falten fills paper reales.
- Revisar `daily_status.json` para saber si el siguiente paso seguro es
  revisar artefactos, resolver blockers o revisar evidencia broker.

### Siguiente bloque

- Acumular al menos 20 sesiones paper broker-confirmed limpias como gate
  operativo y 60 sesiones estables como gate de revision de fase.
- Alimentar `paper-campaign-report` y `paper-performance-report` con
  `--ledger-input reports/tmp/paper_auto_cycle/session_ledger.jsonl`.
- Usar `paper-strategy-quality` para benchmarks simples de baseline vs
  challenger en ETFs con costos y tendencias del ledger, sin promocion
  automatica.
- Usar `paper-phase-review-report` para producir `ACCUMULATING`,
  `READY_FOR_REVIEW` o `BLOCKED`; `READY_FOR_REVIEW` no autoriza live trading.
- Usar `llm-context-pack` como RAG local read-only para runbooks y reportes, sin
  herramientas broker, secretos, bypass de 60 sesiones ni noticias web
  automaticas.

### Mas adelante

- PoC aislado con LEAN o NautilusTrader para comparar paridad research-to-live.
- Series temporales foundation models como challengers, comparados contra
  naive, ARIMA/ETS y LightGBM.
- Futures micro solo como research manifest, sin submit/execute.
- RL o multi-agent trading solo en simulador, con metricas de riesgo y rechazo.

## Checklist antes de aumentar autonomia

- Hay fills paper reales y reconciliados.
- El modelo no fue promovido por backtest aislado.
- El LLM reviewer tiene tests contra secretos, live trading y ordenes directas.
- El autopilot emite recomendaciones, no side effects.
- Los comandos que tocan broker requieren `--confirm-readiness`,
  `--confirm-paper`, `--confirm-auto-submit`, `--confirm-auto-close` y
  `--require-clean-state`.
- `models/latest_model.json` no cambia sin decision humana aprobada.
- Futures no tiene comandos `futures-submit` ni `futures-execute`.
- Toda salida importante queda bajo `reports/tmp` o ruta documentada.

## Guia de uso para futuros agentes

Cuando una tarea pida "hacer el bot mas inteligente", interpretar inteligencia
como mejores evidencias y mejores controles antes que mayor autonomia.

Orden de preferencia:

1. Mejorar datos, tests, backtests y evidencia.
2. Mejorar explicabilidad y revision LLM sin permisos.
3. Mejorar planner determinista.
4. Mejorar paper broker-confirmed.
5. Solo despues considerar mas mercados o mas autonomia.

Si una propuesta implica live trading, credenciales live, envio autonomo de
ordenes, incremento de riesgo, futures execution o secretos en contexto LLM,
debe bloquearse y documentarse como fuera de alcance.

## Fuentes principales

- QuantConnect LEAN: <https://github.com/QuantConnect/Lean>
- NautilusTrader: <https://github.com/nautechsystems/nautilus_trader>
- Freqtrade: <https://github.com/freqtrade/freqtrade>
- FreqAI: <https://www.freqtrade.io/en/stable/freqai/>
- Hummingbot: <https://github.com/hummingbot/hummingbot>
- FinGPT: <https://arxiv.org/abs/2306.06031>
- FinRobot: <https://arxiv.org/abs/2405.14767>
- TradingAgents: <https://arxiv.org/abs/2412.20138>
- LiveTradeBench: <https://arxiv.org/abs/2511.03628>
- QuantCode-Bench: <https://arxiv.org/abs/2604.15151>
- BacktestBench: <https://arxiv.org/abs/2605.17937>
- TimesFM: <https://github.com/google-research/timesfm>
- Chronos: <https://github.com/amazon-science/chronos-forecasting>
- OpenAI reasoning models and Responses guidance:
  <https://developers.openai.com/api/docs/guides/latest-model#using-reasoning-models>
- OpenAI Structured Outputs:
  <https://developers.openai.com/api/docs/guides/structured-outputs>
- OpenAI tools and Agents SDK guidance:
  <https://developers.openai.com/api/docs/guides/tools>
- MLflow Model Registry:
  <https://mlflow.org/docs/latest/ml/model-registry/>
- Optuna docs: <https://optuna.readthedocs.io/en/stable/>
- NFA BASIC: <https://www.nfa.futures.org/basicnet/>
- FINRA Regulatory Notice 15-09:
  <https://www.finra.org/rules-guidance/notices/15-09>
