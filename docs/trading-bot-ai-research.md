# Investigacion y recomendacion: bot de trading con IA para futuros, FX y acciones

Fecha de preparacion: 2026-06-16  
Supuesto regulatorio inicial: operacion desde Estados Unidos o con brokers que acepten clientes bajo reglas de EE. UU. Si la residencia, entidad legal o pais operativo cambia, hay que revisar disponibilidad de productos, permisos, impuestos y restricciones.

## 1. Resumen ejecutivo

La recomendacion practica es construir el sistema en fases, empezando por un MVP en acciones/ETFs o futuros micro altamente liquidos, no por un bot LLM que opere directamente. La arquitectura debe separar tres responsabilidades:

1. Motor determinista de trading: datos, calendario, ordenes, posiciones, riesgo, auditoria, backtesting y paper/live trading.
2. Modelos predictivos: modelos estadisticos, ML tabular, modelos de series temporales y, eventualmente, aprendizaje por refuerzo solo despues de tener controles maduros.
3. LLM: asistente de investigacion, analisis de noticias/reportes, generacion de hipotesis, explicacion de decisiones y supervision, pero no autoridad final para enviar ordenes sin reglas y limites verificables.

Recomendacion de camino:

1. **Base tecnica principal:** QuantConnect LEAN + Interactive Brokers si el objetivo final es multi-activo serio: acciones/ETFs, futuros, FX, opciones y paper/live trading. LEAN es abierto, tiene CLI local, backtesting y live trading; IBKR da acceso multi-activo, aunque exige gestionar TWS/IB Gateway, permisos y datos.
2. **MVP mas facil:** Alpaca para acciones/ETFs en paper trading, porque reduce friccion de API y credenciales. Es el mejor camino si se quiere validar pipeline, entrenamiento dinamico y monitoreo antes de tocar futuros.
3. **Primer mercado de futuros recomendado:** Micro E-mini de indices accionarios de CME, especialmente MES/MNQ/M2K/MYM segun liquidez y costos. Tienen buena liquidez, tamanos mas manejables, mercado regulado y relacion directa con indices ampliamente analizados.
4. **Futuros Forex / FX futures:** buenos como segunda expansion de futuros, no como primer producto. Son regulados y transparentes, pero la microestructura, horarios, rolls y sensibilidad macro los vuelven mas exigentes que acciones/ETFs y algunos indices.
5. **Forex spot:** no lo recomendaria como primera implementacion robusta. Es facil encontrar APIs, pero el entorno OTC, spreads, ejecucion, apalancamiento y diferencias por broker complican la validacion.
6. **Crypto perps/futuros:** solo como fase posterior y preferiblemente en venues regulados disponibles para EE. UU. Tienen alta volatilidad y oportunidad bruta, pero mayor riesgo operativo, legal, de liquidez y de regimen.

Sobre rentabilidad: no hay forma responsable de afirmar cual sera "mas rentable" antes de backtests sin sesgo, paper trading y validacion out-of-sample. Como hipotesis de trabajo, el mejor balance entre oportunidad, liquidez, costos y complejidad para un bot robusto seria: **Micro E-mini equity index futures despues de validar el pipeline en acciones/ETFs**. El camino mas facil manteniendo calidad es: **acciones/ETFs primero, futuros micro despues**.

## 2. Respuesta directa: puedo generar, entrenar e implementar este modelo?

Si. Puedo ayudarte a:

- disenar la arquitectura;
- generar el codigo base;
- crear pipelines de datos;
- implementar backtesting, walk-forward, paper trading y deployment;
- integrar brokers via APIs;
- entrenar modelos locales si hay datos, hardware y dependencias;
- construir evaluaciones para evitar sobreentrenamiento;
- instrumentar monitoreo, alertas, kill-switches y auditoria.

No debo prometer rentabilidad ni sustituir asesoria financiera, legal, fiscal o regulatoria. La decision de operar en vivo requiere cuenta de broker, permisos, capital de riesgo, datos de mercado, aceptacion de terminos del broker y controles de perdida.

## 3. Ranking por mercado

Escala: 1 = bajo/desfavorable, 5 = alto/favorable. "Potencial" significa oportunidad ajustada por estructura de mercado y capacidad de ejecucion, no retorno garantizado.

| Mercado | Facilidad de implementacion | Robustez operativa | Potencial ajustado | Riesgo de sobreentrenamiento | Recomendacion |
| --- | ---: | ---: | ---: | ---: | --- |
| Acciones/ETFs EE. UU. | 5 | 4 | 3 | 3 | Mejor laboratorio inicial. Permite validar datos, senales, riesgo y paper trading con menor complejidad. |
| Micro E-mini equity index futures | 3.5 | 4 | 4 | 4 | Mejor primer mercado de futuros. Liquido, regulado, apalancado y con tamanos micro. |
| FX futures CME | 3 | 4 | 3.5 | 4 | Buena segunda fase. Mas dependiente de macro, horarios globales y gestion de roll. |
| Forex spot | 3 | 2.5 | 3 | 4 | No recomendado como primer camino si la prioridad es robustez institucional. Util solo si se requiere FX spot. |
| Crypto futures/perps regulados | 2.5 | 2.5 | 4.5 bruto / 2.5 ajustado | 5 | Fase posterior. Solo tras validar legalidad, venue, liquidez, custodia y controles. |
| Futuros de acciones individuales | 1.5 | 2 | incierto | 4 | No usar en MVP. Confirmar disponibilidad, liquidez y aprobaciones regulatorias antes de considerarlos. |

Conclusion por criterio:

- **Mas facil con calidad:** acciones/ETFs via Alpaca o LEAN paper.
- **Mejor balance riesgo/implementacion/rentabilidad potencial:** Micro E-mini equity index futures.
- **Mas institucional multi-activo:** LEAN + IBKR.
- **Mas rapido para experimentar con IA local:** acciones/ETFs, datos diarios/horarios, modelos tabulares y time-series.
- **Mayor riesgo de falsas conclusiones:** cripto perps, forex spot y estrategias intradia de alta frecuencia con datos pobres.

## 4. Mercados: detalle de decision

### 4.1 Acciones y ETFs

Ventajas:

- APIs mas simples que futuros y FX.
- Universos amplios, buena documentacion, paper trading accesible.
- Permiten comenzar con estrategias de menor frecuencia: diario, 4h, 1h.
- Mas facil auditar dividendos, splits, universos, costes y posiciones si se usa un proveedor serio.

Desventajas:

- Menor apalancamiento que futuros; rentabilidad bruta esperada puede ser menor.
- Sesgos comunes: survivorship bias, corporate actions mal aplicadas, seleccion retrospectiva de activos ganadores.
- Mayor competencia en senales simples.

Uso recomendado:

- MVP de infraestructura.
- Estrategias long-only, long/cash, rotacion de ETFs, market regime, pairs/market-neutral simple.
- Paper trading durante al menos 60-90 dias antes de mover a futuros.

### 4.2 Futuros de indices accionarios

Ventajas:

- Mercado regulado, centralizado y con buena liquidez en productos principales.
- Micro E-mini permite menor notional que E-mini estandar.
- Costos y ejecucion mas transparentes que FX spot.
- Apalancamiento eficiente, pero controlable con sizing conservador.

Desventajas:

- Roll de contratos, margenes, horarios, limites de perdida y cambios de volatilidad.
- El apalancamiento puede destruir cuentas rapido si el sizing o kill-switch fallan.
- Datos de calidad cuestan mas si se quiere intradia realista.

Uso recomendado:

- Primera expansion despues de acciones/ETFs.
- Empezar con MES y MNQ; agregar M2K/MYM si hay razon estadistica.
- Evitar scalping y HFT al inicio; usar marcos 15m/1h/diario hasta tener ejecucion probada.

### 4.3 Futuros Forex / FX futures

Ventajas:

- CME opera un mercado FX regulado y listado.
- Buena transparencia relativa frente a FX spot OTC.
- Productos G10 con liquidez razonable en contratos principales.

Desventajas:

- Senales altamente dependientes de macro, tasas, eventos, bancos centrales y sesiones globales.
- Roll y calendarios deben estar bien modelados.
- Menor facilidad que indices para validar senales basadas solo en precio.

Uso recomendado:

- Fase 3, despues de tener motor de futuros funcionando.
- Empezar con 6E, 6J, 6B, 6A o micro FX si hay liquidez suficiente para el tamano de cuenta.
- Usar modelos con features macro/calendario, no solo indicadores tecnicos.

### 4.4 Forex spot

Ventajas:

- APIs REST/FIX disponibles en brokers como OANDA.
- Cuenta demo, datos historicos y precios 24h.
- Bueno para experimentar si el objetivo especifico es FX spot.

Desventajas:

- No es un mercado centralizado; ejecucion y spreads dependen del broker.
- Alto apalancamiento y diferencias regulatorias por pais.
- Backtests pueden fallar al pasar a vivo por spreads, slippage, rollover y reglas del broker.

Uso recomendado:

- No como primer MVP robusto.
- Considerarlo solo si se define un broker concreto, restricciones de cuenta y modelo de costes realista.

### 4.5 Crypto futures/perps

Ventajas:

- Volatilidad alta, mercado 24/7 y datos abundantes.
- Frameworks abiertos como Freqtrade/FreqAI y Hummingbot tienen mucha traccion en cripto.
- Coinbase Derivatives ofrece productos regulados en EE. UU. y APIs para derivados.

Desventajas:

- Riesgo legal/regulatorio y de disponibilidad por jurisdiccion.
- Regimenes cambian rapido; alta probabilidad de sobreentrenamiento.
- Funding, liquidez, liquidaciones, colas, mantenimiento y fallos de venue son parte del modelo.

Uso recomendado:

- Fase posterior.
- Solo con paper/sandbox, limites duros, capital pequeno y venues regulados o de riesgo aceptado.

## 5. Brokers y venues candidatos

| Broker/venue | Activos relevantes | Fortalezas | Debilidades | Recomendacion |
| --- | --- | --- | --- | --- |
| Interactive Brokers | Acciones, ETFs, futuros, FX, opciones, bonos, fondos | Cobertura multi-activo, APIs, paper trading, integracion LEAN/QuantConnect | TWS/IB Gateway, permisos, datos de mercado, 2FA, limites de API, curva operativa | Mejor broker principal para arquitectura multi-activo. |
| Alpaca | Acciones/ETFs, cripto segun disponibilidad | API simple, paper trading gratuito, buena velocidad para MVP | No cubre futuros CME ni FX futures; mercado mas limitado | Mejor MVP si se empieza con acciones/ETFs. |
| NinjaTrader / Tradovate | Futuros | Enfoque fuerte en futuros retail, ecosistema maduro | Lock-in de plataforma, C#/NinjaScript o APIs especificas | Candidato si el proyecto se limita a futuros CME. |
| OANDA | Forex spot/CFD segun jurisdiccion | REST v20, demo, historicos desde 2005 segun docs | FX/CFD de alto riesgo, ejecucion depende del broker, no equivale a FX futures CME | Candidato solo si se decide operar FX spot. |
| Coinbase Derivatives | Futuros/perps regulados en EE. UU., indices cripto/equity segun producto | DCM registrado, APIs REST/FIX/SBE/UDP, productos 24/7 | Producto y acceso deben verificarse por cuenta/jurisdiccion; ecosistema mas nuevo | Fase posterior, no MVP. |

Validacion regulatoria minima:

- Revisar el broker en NFA BASIC si es FCM/RFED/NFA member.
- Confirmar permisos por producto: futuros, FX, margin, options, crypto derivatives.
- Confirmar si los datos de mercado permiten uso automatizado y almacenamiento historico.
- Revisar reglas de margin, liquidacion, pattern day trading, short selling y restricciones de API.

## 6. Frameworks abiertos y herramientas

| Herramienta | Mejor uso | Ventajas | Riesgos/debilidades | Veredicto |
| --- | --- | --- | --- | --- |
| QuantConnect LEAN | Backtesting/live multi-activo | Open source, Python/C#, CLI local, paper/live, brokers y datasets | Complejidad inicial; algunas integraciones/datos dependen de cuenta | Recomendado como backbone principal. |
| NautilusTrader | Motor profesional multi-venue, trading sistematico | Rust core, arquitectura event-driven, backtest/live parity | Curva alta; integraciones dependen de adaptadores | Muy bueno para arquitectura avanzada; no primer MVP si se busca rapidez. |
| Freqtrade + FreqAI | Cripto spot/futures con ML | Retraining automatico, feature engineering, backtesting, protecciones | Enfocado en cripto; riesgo de overfit y venue risk | Util solo si el camino cripto se vuelve prioritario. |
| Hummingbot | Market making/estrategias cripto | Arquitectura modular Strategy V2, controllers, conectores cripto | Mas orientado a cripto y liquidez/market making | Complementario, no core multi-activo. |
| FinRL-X | Investigacion AI/DRL/portfolio allocation | Arquitectura modular, paper con Alpaca, patrones ML/DRL | Muy reciente; usar como referencia, no depender en produccion sin hardening | Fuente de ideas, no backbone inicial. |
| OpenBB | Data/research layer para analysts, quants y AI agents | Integra fuentes financieras y puede exponer datos a Python, REST, Workspace y MCP | Licencia AGPLv3; calidad depende de proveedores; no debe alimentar live trading sin validacion | Util como capa de research/datos, no como motor de ejecucion. |
| Backtrader / vectorbt / pandas | Research rapido local | Simples para prototipos y notebooks | Brecha research-to-live; riesgo de simular ejecucion pobre | Usar para exploracion, no como motor live principal. |

Recomendacion de stack:

- **Backbone:** LEAN.
- **Research local:** notebooks Python + pandas/polars + scikit-learn/LightGBM/XGBoost + PyTorch.
- **Series temporales:** TimesFM y Chronos/Chronos-2 como challengers de forecast, comparados contra modelos simples.
- **LLM financiero:** FinGPT, Qwen3/DeepSeek-R1/Gemma local o LLM con RAG para noticias/reportes/sentiment; no para ejecucion directa.
- **Modelo registry:** MLflow.
- **Monitoreo ML/data drift:** Evidently.
- **Optimizacion:** Optuna, con limites estrictos para evitar sobreentrenamiento.
- **Feature store simple:** parquet versionado al inicio; Feast solo si crece la complejidad.
- **Orquestacion:** cron/systemd al inicio; Airflow/Prefect si hay multiples pipelines.
- **Observabilidad:** logs estructurados, metricas Prometheus/Grafana o un panel simple, alertas por drawdown, latencia, errores de broker y drift.

### 6.1 Skills, plugins y MCP a considerar

El criterio principal no es "cuantas capacidades instalamos", sino que cada herramienta tenga permisos minimos, trazabilidad y valor claro. Para este proyecto ya existen skills suficientes para trabajar bien: `openai-docs`, `jupyter-notebook`, `cli-creator`, `security-best-practices`, `security-threat-model`, `sentry`, `playwright`, `pdf` y `define-goal`.

| Categoria | Herramienta/capacidad | Uso recomendado | Decision |
| --- | --- | --- | --- |
| Skills Codex oficiales | `openai-docs`, `jupyter-notebook`, `cli-creator`, `security-*`, `sentry` | Desarrollo LLM, notebooks, CLIs, auditoria y observabilidad | Mantener como base. |
| MCP lectura | `filesystem`, `git`, `fetch`, `time`, `memory` | Lectura controlada de archivos, repo, web y memoria de proyecto | Usar solo con allowlists y permisos minimos. |
| MCP datos | `postgres`/`sqlite` read-only | Consultar datasets, features y resultados | Permitido solo read-only; writes por pipeline versionado. |
| MCP broker/trading | Alpaca/IBKR/otros si existen | Paper trading y consulta de cuenta | No usar al inicio; si se prueba, paper-only, allowlist de simbolos y sin credenciales live. |
| Skill TimesFM | `timesfm-forecasting` del repo de Google Research | Forecasting zero-shot de series temporales | Candidato a auditar; no instalar globalmente sin fijar commit. |
| Plugins UI/dashboard | Dashboards o visualizaciones | Monitoreo de backtest/paper/live | Utiles si consumen datos saneados, no broker live directo. |

Politica de instalacion de terceros:

- revisar `SKILL.md`, scripts, dependencias y permisos antes de instalar;
- fijar commit/tag, no ramas flotantes;
- ejecutar primero en sandbox;
- prohibir acceso simultaneo a `.env`, red externa y broker live;
- usar cuentas paper/read-only por defecto;
- documentar cada instalacion en `docs/codex-capabilities-for-trading-bot.md`.

La revision ampliada esta en [docs/third-party-skills-plugins-open-source-ai-review.md](/home/adquiod/Documentos/Algoritmic-IA/docs/third-party-skills-plugins-open-source-ai-review.md).

### 6.2 Modelos IA open source/open-weight a considerar

| Familia | Uso recomendado | Riesgo/nota |
| --- | --- | --- |
| Qwen3 | RAG, tool-use local, razonamiento, analisis de logs/noticias | Buen primer candidato local si hay GPU; revisar tamano y cuantizacion. |
| DeepSeek-R1/distilled | Razonamiento offline y generacion de hipotesis | Evaluar safety, sesgos y consistencia antes de integrarlo. |
| Gemma 4 E2B/E4B o Gemma 3 1B/4B | Modelos pequenos/medianos locales | Buena opcion si el hardware es limitado; revisar licencia por version y preferir Gemma 4 E2B/E4B para pruebas nuevas. |
| Llama 4 | Analisis multimodal/contexto largo | Licencia Meta; validar uso permitido antes de produccion. |
| Mistral/Mixtral | Baseline local y tareas generales | Revisar licencia por modelo; no todos los modelos nuevos son Apache/comerciales. |
| FinGPT | Sentiment financiero, headlines, NER, extraccion de eventos | Usar como feature secundaria, nunca como trader autonomo. |
| FinRobot | Research financiero y reportes | Util para analisis cualitativo/equity research, no para ejecucion live. |
| TimesFM / Chronos | Forecasting de precio, volatilidad o variables auxiliares | Usar como challengers contra baselines, no como fuente unica de senales. |

Orden recomendado de pruebas:

1. Baseline determinista: momentum + volatility targeting.
2. ML tabular: LightGBM/XGBoost/CatBoost.
3. Forecasting foundation: TimesFM/Chronos.
4. LLM financiero: FinGPT o Qwen3/RAG para sentiment/eventos.
5. RL: solo experimental cuando exista simulador realista.

### 6.3 Modelos pequenos entrenables para especializar el bot

Para este proyecto conviene priorizar modelos pequenos, auditables y baratos de ajustar. No buscamos entrenar un LLM desde cero; buscamos especializar modelos existentes con LoRA/QLoRA, SFT ligero, fine-tuning de clasificadores, contrastive learning para embeddings y entrenamiento supervisado de modelos tabulares.

| Modelo | Tamano aproximado | Licencia/estado | Rol recomendado | Como especializarlo |
| --- | ---: | --- | --- | --- |
| Qwen3-1.7B | 1.7B | Apache 2.0 | LLM local pequeno para resumen, explicacion, clasificacion asistida y RAG | LoRA/QLoRA con ejemplos de noticias, logs, tesis de trade y explicaciones etiquetadas. |
| Qwen3-4B | 4B | Apache 2.0 | Mejor balance pequeno/capaz para razonamiento, tool-use y analisis financiero | LoRA/QLoRA; preferido si hay GPU de 12-24 GB o cuantizacion 4-bit. |
| SmolLM3-3B | 3B | Apache 2.0 | LLM pequeno, abierto, multilingue y long-context | SFT/LoRA para analisis en espanol/ingles, resumen de reportes y explicaciones. |
| Phi-4-mini-instruct | 3.8B/4B | MIT | Razonamiento pequeno con contexto largo | Fine-tuning con TRL/Accelerate; buen candidato para RAG y herramientas, revisar factualidad. |
| IBM Granite 3.3 2B Instruct | 2B | Apache 2.0 | Modelo empresarial pequeno para RAG, clasificacion, extraccion y function calling | LoRA/SFT sobre prompts de auditoria, logs y documentacion del bot. |
| Gemma 4 E2B/E4B o Gemma 3 1B/4B | 2B/4B efectivos o 1B/4B | Gemma license | Modelo pequeno local si hardware es limitado; Gemma 4 agrega mejor contexto y modelos QAT | LoRA/QLoRA solo tras validar terminos de uso; bueno para prototipos locales y edge. |
| Mistral 7B Instruct | 7B | Apache 2.0 en Mistral 7B original | Baseline robusto 7B para generacion/razonamiento | QLoRA si se requiere mas capacidad; mayor costo que 1-4B. |
| FinBERT / finbert-tone | ~110M | Apache 2.0 en repo FinBERT; verificar cada checkpoint | Sentiment/tone financiero, headlines, filings, earnings calls | Fine-tuning supervisado con labels propios de impacto: bullish/bearish/neutral, riesgo, evento macro. |
| ProsusAI FinBERT | ~110M | checkpoint HF sin licencia explicita en la tarjeta; revisar antes de produccion | Sentiment financiero rapido | Usar como baseline; preferir checkpoints con licencia clara para produccion. |
| Qwen3-Embedding-0.6B | 0.6B | Apache 2.0 | Embeddings/reranking para RAG financiero multilingue | Fine-tuning contrastivo con pares pregunta-documento, noticias-evento y logs-incidente. |
| BGE-small-en-v1.5 | 33M | MIT via FlagEmbedding | Embeddings pequenos y baratos | Buen baseline para RAG local; reentrenar/fine-tunear si el dominio lo exige. |
| all-MiniLM-L6-v2 | ~22M | Apache 2.0 | Embeddings muy ligeros | Baseline rapido para busqueda semantica; no suficiente para documentos financieros complejos. |
| Chronos-Bolt tiny/mini/small | 9M/21M/48M | Apache 2.0 | Forecasting ligero de series temporales | Benchmark contra momentum/LightGBM; no usar como senal unica. |
| Chronos-2 small | 28M | Apache 2.0 | Forecasting con covariables | Challenger barato para volatilidad, rango, retornos y variables auxiliares. |
| TimesFM 2.5 | 200M | Apache 2.0 | Forecasting zero-shot/fine-tuning de series | Probar con LoRA/fine-tuning si aporta frente a baselines y Chronos. |

Recomendacion por rol:

- **Generativo/RAG local:** Qwen3-4B primero; Qwen3-1.7B si el hardware es limitado; Granite 2B si se prioriza licencia empresarial clara; Gemma 4 E2B/E4B si se quiere probar despliegue muy eficiente.
- **Sentiment y clasificacion financiera:** FinBERT/finbert-tone como primer modelo especializado; despues comparar contra un LLM pequeno con LoRA.
- **Embeddings para RAG:** Qwen3-Embedding-0.6B si se necesita calidad multilingue y contexto largo; BGE-small o all-MiniLM para prototipo barato.
- **Forecasting:** Chronos-Bolt small/Chronos-2 small como opcion ligera; TimesFM 2.5 como challenger mas pesado.
- **No recomendado al inicio:** entrenar Llama/Gemma/Mistral grandes o RL profundo antes de tener datos, backtest y paper trading confiables.

Flujo de entrenamiento recomendado:

1. Crear datasets pequenos y versionados: noticias/eventos, logs del bot, decisiones simuladas, etiquetas de impacto, pares pregunta-documento y series OHLCV.
2. Entrenar primero modelos baratos: FinBERT para sentiment, BGE/Qwen embedding para RAG, LightGBM para senales tabulares.
3. Ajustar un LLM pequeno con LoRA/QLoRA solo cuando exista un dataset de instrucciones propio y una evaluacion automatica.
4. Evaluar con tareas separadas: sentiment, extraccion de eventos, explicacion de trades, busqueda RAG, forecast, impacto en backtest.
5. Registrar todo en MLflow: dataset hash, modelo base, adaptador LoRA, parametros, metricas, fecha y version.
6. Promover modelos por champion/challenger; ningun modelo pequeno especializado debe tocar ordenes sin pasar por risk gates.

## 7. Arquitectura recomendada

Flujo objetivo:

```text
Fuentes de datos
  -> ingestion versionada
  -> limpieza y normalizacion
  -> features y labels
  -> validacion sin leakage
  -> entrenamiento walk-forward
  -> registry de modelos
  -> backtest realista
  -> paper trading
  -> risk gate
  -> broker adapter
  -> monitoreo y auditoria
```

Componentes:

1. **Data ingestion**
   - Datos OHLCV, trades, quotes, fundamentals, calendarios, eventos macro y noticias si aplica.
   - Versionar datasets para reproducibilidad.
   - Separar datos "conocidos en ese momento" de datos revisados posteriormente.

2. **Feature engineering**
   - Precio/volumen: momentum, volatility, trend, breakout, mean reversion.
   - Microestructura si se usan datos intradia: spread, imbalance, volume profile, session features.
   - Regimen: volatilidad realizada, VIX, tasas, dolar, calendario FOMC/CPI/NFP para FX futures.
   - Sentiment/news: solo como feature secundaria y con timestamp estricto.

3. **Modelos**
   - Baseline sin IA: reglas simples, momentum/volatility targeting.
   - ML tabular: logistic/linear models, random forest, gradient boosting.
   - Series temporales: Chronos/Chronos-2 o modelos propios, siempre comparados contra naive forecasts.
   - LLM: clasificacion/sintesis de noticias, extraccion de eventos, explicacion y generacion de hipotesis.
   - DRL: solo si el sistema ya tiene simulador realista, costos, slippage y limites de riesgo.

4. **Portfolio/risk**
   - No operar "senales" directamente; convertirlas en pesos/posiciones con limites.
   - Volatility targeting.
   - Max exposure por activo, sector, moneda y clase de activo.
   - Max daily loss, max drawdown, max leverage, max order size.
   - Kill-switch tecnico y financiero.

5. **Execution**
   - Ordenes limit/market segun liquidez y horizonte.
   - Slippage modelado y medido.
   - Reconciliacion de posiciones broker vs estado interno.
   - Idempotencia: evitar duplicar ordenes por reintentos.

6. **Monitoring**
   - PnL, drawdown, exposicion, latencia, rechazos de orden, desconexiones, drift de features, drift de performance.
   - Alertas y auto-disable si se viola un limite.

## 8. Entrenamiento dinamico sin sobreentrenamiento

Principios:

- Retraining programado, no continuo sin control. Por ejemplo semanal para modelos diarios, diario para intradia de baja frecuencia, nunca "cada tick" en el MVP.
- Walk-forward validation con ventanas fijas y periodos out-of-sample.
- Purged/embargoed cross-validation cuando las etiquetas se solapan en el tiempo.
- Deflated Sharpe Ratio o pruebas equivalentes para penalizar seleccion de multiples estrategias.
- Modelos simples como baseline obligatorio. Un modelo complejo solo pasa si supera al baseline despues de costos.
- Separar tuning, validacion y paper trading.
- Congelar el modelo durante el paper test; los cambios deben entrar por version nueva/challenger.
- Evaluar por regimen: tendencia, rango, alta volatilidad, baja volatilidad, crisis, eventos macro.

Gates minimos antes de paper:

- Backtest reproduce resultados con semilla fija y version de datos.
- Costos, slippage, comisiones y funding/margin incluidos segun mercado.
- Ninguna feature usa informacion futura.
- Resultado no depende de pocos trades.
- Drawdown maximo aceptable definido antes del test.
- Stress test: gaps, datos faltantes, rechazos de orden, desconexion broker.

Gates minimos antes de live:

- Paper trading 60-90 dias para acciones/ETFs; 90+ dias o al menos varios ciclos de volatilidad para futuros.
- Diferencia paper vs backtest explicada.
- Kill-switch probado.
- Reconciliacion broker probada.
- Limite de capital inicial pequeno y escalado por hitos.
- Revision manual de cada cambio de modelo.

## 9. Rol correcto de LLM y modelos locales

LLM recomendado:

- RAG sobre documentacion de estrategias, logs, reportes de broker y fuentes de mercado.
- Resumen de noticias/eventos y clasificacion de impacto potencial.
- Generacion de hipotesis para research, que luego deben pasar por backtest.
- Explicacion de decisiones ya tomadas por el motor determinista.
- Auditor de anomalias: "por que el bot no opero", "por que aumento riesgo", "que cambio en el modelo".

LLM no recomendado:

- Decidir ordenes directamente desde texto libre.
- Aumentar apalancamiento sin reglas deterministas.
- Cambiar parametros en vivo sin validacion.
- Usar noticias sin timestamp/verificacion.

Modelo local especializado:

- Comenzar con modelos tabulares y de series temporales entrenados localmente.
- Guardar cada modelo con metadata: dataset, rango temporal, features, parametros, metricas, commit/hash, entorno.
- Mantener champion/challenger: un modelo nuevo no reemplaza al actual hasta superar criterios definidos.
- Usar LLM local/FinGPT para lenguaje financiero solo si agrega informacion medible sobre baseline.

## 10. Datos: prioridad y proveedores

Para acciones/ETFs:

- Alpaca/IBKR para paper/live y datos basicos.
- Polygon, Databento, IQFeed o QuantConnect datasets si se requiere mayor calidad.
- Controlar splits, dividendos, delistings y survivorship bias.

Para futuros CME:

- Databento, CME DataMine, CQG/Rithmic/Tradovate/NinjaTrader/IBKR segun broker.
- Necesario modelar contratos continuos, roll, tick size, sesiones, margenes y comisiones.
- Para microestructura, datos order book/trades son preferibles a barras reconstruidas.

Para FX futures:

- Datos CME/Databento/IBKR.
- Agregar calendario macro: FOMC, CPI, NFP, bancos centrales, tasas y dolar.

Para forex spot:

- OANDA u otro broker especifico.
- Backtest debe usar spreads historicos o proxy realista, swaps/rollover y horarios del broker.

Para cripto derivados:

- Coinbase Derivatives si se prioriza venue regulado en EE. UU.
- Modelar funding, fees, liquidaciones y eventos 24/7.

## 11. Plan de implementacion recomendado

### Fase 0: decisiones y compliance

Duracion estimada: 1 semana.

Entregables:

- Elegir broker inicial: Alpaca para MVP rapido o IBKR para camino definitivo.
- Confirmar jurisdiccion, entidad, productos permitidos y permisos de mercado.
- Definir capital maximo de prueba, perdida diaria maxima y perdida mensual maxima.
- Elegir horizonte: diario/1h/15m. Recomendado: diario/1h al inicio.
- Definir politica de secrets: ningun modelo, skill o MCP debe leer credenciales live sin necesidad explicita.

Criterio de salida:

- Broker paper listo.
- Datos historicos definidos.
- Politica de riesgo escrita.
- Politica de permisos para tools/skills/MCP escrita.

### Fase 0.5: seleccion y auditoria de skills, plugins y herramientas

Duracion estimada: 3-5 dias.

Entregables:

- Inventario de skills Codex oficiales ya instaladas y su uso en el proyecto.
- Lista de candidatos de terceros: TimesFM skill, MCP reference servers, OpenBB, FinGPT, FinRobot, FinRL-X, MLflow, Evidently, Optuna.
- Auditoria manual de cualquier `SKILL.md` externo antes de instalarlo.
- Matriz de permisos: read-only, paper-only, write, broker-live prohibido por defecto.
- Decision de dependencias Fase A: `pandas/polars`, `pyarrow`, `scikit-learn`, `lightgbm/xgboost`, `mlflow`, `optuna`, `evidently`, `pandera`, `jupyter`.
- Decision de dependencias Fase B: `torch`, `transformers`, `accelerate`, `peft`, `trl`, `sentence-transformers`, `datasets`, `bitsandbytes` si hay GPU compatible, `timesfm`, `chronos-forecasting`, `neuralforecast` o `darts`.
- Seleccion de modelos pequenos por rol: generativo/RAG, sentiment, embeddings/reranking y forecasting.
- Definicion de datasets de fine-tuning: noticias/eventos, logs del bot, explicaciones de trades, pares RAG y series temporales.

Criterio de salida:

- Ninguna skill/plugin de terceros instalada sin revision.
- Ningun MCP con acceso a broker live.
- Lista de dependencias aprobada y documentada.
- Checklist anti-supply-chain aprobado para futuras instalaciones.
- Modelo pequeno inicial elegido con criterio de hardware, licencia y evaluacion.

### Fase 1: research/backtest reproducible

Duracion estimada: 2-4 semanas.

Entregables:

- Repo estructurado.
- Ingestion versionada.
- Backtest reproducible.
- Baseline no-ML.
- Primer modelo ML simple.
- MLflow local para experiment tracking y model registry.
- Evidently o reporte equivalente para calidad/drift de datos.
- Optuna solo con limites de trials y walk-forward, no como optimizador libre.
- Reporte de metricas: CAGR, Sharpe, Sortino, max drawdown, turnover, costos, hit rate, exposure, trade count.

Criterio de salida:

- Baseline y modelo comparados out-of-sample.
- Costos incluidos.
- Sin leakage evidente.
- Registro de experimentos reproducible.
- Dependencias fijadas por version.

### Fase 2: paper trading en acciones/ETFs

Duracion estimada: 2-3 meses.

Entregables:

- Adapter broker paper.
- Reconciliacion posiciones/ordenes.
- Dashboard minimo.
- Alertas.
- Model registry.
- Pipeline de retraining controlado.

Criterio de salida:

- 60-90 dias de paper sin fallos graves.
- Diferencias paper/backtest explicadas.
- Kill-switch probado.

### Fase 3: futuros micro de indices

Duracion estimada: 1-2 meses despues de Fase 2.

Entregables:

- Contratos continuos y roll.
- Margen y sizing por contrato.
- Slippage por tick.
- Soporte de MES/MNQ y, si aplica, M2K/MYM.
- Simulacion de gaps y eventos.

Criterio de salida:

- Paper futures estable.
- Riesgo por trade y por dia bajo limites.

### Fase 4: FX futures

Duracion estimada: 1-2 meses despues de Fase 3.

Entregables:

- Calendario macro.
- Roll de FX futures.
- Features de tasas/dolar/regimen.
- Modelos separados por par o modelo multi-instrumento.

Criterio de salida:

- Mejora demostrada frente a baseline y frente a indices en terminos ajustados por riesgo.

### Fase 5: live controlado

Duracion estimada: gradual.

Entregables:

- Capital pequeno.
- Limites duros.
- Revision diaria/semanal.
- Escalado solo por hitos.

Criterio de salida:

- Estabilidad operacional y perdida maxima dentro de tolerancia.

## 12. Decision recomendada ahora

Si el objetivo principal es tomar la mejor decision informada y avanzar con robustez, recomiendo:

1. **Elegir LEAN + IBKR como arquitectura objetivo.** Es el camino mas serio para multi-activo: acciones, ETFs, futuros, FX futures y eventualmente otros productos.
2. **Construir el MVP en acciones/ETFs aunque el destino sea futuros.** Reduce variables y permite probar todo lo dificil: datos, entrenamiento, backtest, paper, monitoreo, risk gates y actualizacion de modelos.
3. **Pasar a Micro E-mini equity index futures como primer producto de futuros.** Es el mejor punto medio entre oportunidad, liquidez, datos, regulacion y tamanos operables.
4. **Agregar FX futures despues.** Tienen sentido como diversificacion, no como primer paso.
5. **No empezar por forex spot ni crypto perps.** Pueden ser rentables, pero son peores como primer objetivo si se prioriza robustez, mantenimiento y control de sobreentrenamiento.

Ruta alternativa si se prioriza velocidad:

- MVP con Alpaca para acciones/ETFs.
- Cuando el pipeline este maduro, migrar o duplicar estrategia en LEAN + IBKR para futuros.

Ruta alternativa si se prioriza solo futuros:

- LEAN + IBKR o NinjaTrader/Tradovate desde el dia uno.
- Empezar con MES/MNQ.
- Aceptar que la curva inicial sera mas dura que en acciones/ETFs.

## 13. Preguntas que faltan para cerrar el diseno tecnico

1. Pais/residencia y tipo de cuenta: personal, empresa, prop, broker permitido.
2. Capital inicial de prueba y perdida maxima aceptable.
3. Horizonte de trading: diario, swing, intradia 1h/15m, scalping.
4. Preferencia de broker: IBKR, Alpaca, NinjaTrader/Tradovate, otro.
5. Presupuesto mensual para datos.
6. Hardware disponible para entrenamiento local: CPU, GPU, RAM, almacenamiento.
7. Tolerancia a cloud vs local.
8. Si se requiere que el modelo sea interpretable.

## 14. Proxima accion tecnica sugerida

Crear un prototipo base con esta estructura:

```text
trading-ai/
  data/
  docs/
  research/
  notebooks/
  models/
  strategies/
  execution/
  risk/
  monitoring/
  configs/
  tests/
```

Artefactos iniciales recomendados:

- `pyproject.toml` con dependencias Fase A fijadas.
- `docs/tooling-risk-register.md` para registrar skills, plugins, MCP y dependencias con permisos.
- `docs/model-evaluation-policy.md` para definir gates de modelos, walk-forward, overfitting y champion/challenger.
- `configs/permissions.yml` para separar read-only, paper-only y live-prohibited.
- `notebooks/benchmark_baseline_vs_ml_vs_timeseries.ipynb` para comparar baseline, LightGBM/XGBoost y TimesFM/Chronos.
- MLflow local para experiment tracking.
- Evidently para reportes de calidad/drift.

Primer MVP recomendado:

- Universo: SPY, QQQ, IWM, TLT, GLD, sector ETFs y efectivo.
- Horizonte: diario o 1h.
- Broker: Alpaca paper si se busca rapidez; IBKR paper si se quiere empezar ya sobre el broker final.
- Modelo baseline: rotacion por momentum + volatilidad objetivo.
- Modelo ML v1: clasificador/regresor tabular para retorno/riesgo futuro.
- Modelo forecasting challenger: TimesFM/Chronos solo como comparador, no como fuente unica de senal.
- LLM v1: analisis offline de logs y resumen de eventos, sin permiso de operar.
- Gate: no live hasta completar paper trading, auditoria de tools/skills y validacion de modelos.

## 15. Implementacion inicial realizada

Estado al 2026-06-16: se implemento el scaffold de investigacion de la
seccion 14 y una primera capa MVP ejecutable por CLI. El alcance sigue
prohibiendo broker live, credenciales live y autoridad operativa del LLM.

Estado al 2026-06-17: se cerro el hito de readiness diaria paper-only con
smoke offline E2E y puerta broker-confirmed desde readiness aprobado.
`prepare-paper-daily --run-offline-smoke` importa o reutiliza un paquete
aprobado, ejecuta evaluacion y registro, genera `paper_daily.generated.yml`
bajo `reports/tmp/paper_daily_prepare/...`, y prueba esa config con
`paper-daily` sin broker, credenciales ni Telegram. `paper-daily-from-readiness`
consume `readiness.json`, exige `status=READY`,
`ready_for_paper_daily=true`, `exit_code=0`, smoke offline solicitado/corrido
con exit `0` y confirmaciones manuales, y solo entonces ejecuta `paper-daily`
contra Alpaca paper con rutas bajo `paper_daily/broker_confirmed/`. El wrapper
preserva los artefactos del smoke offline, escribe `broker_run.json`/`.md`,
fuerza `send_telegram=false`, propaga los codigos `0`/`1`/`2` de `paper-daily`
cuando lo ejecuta, y mantiene el alcance paper-only: no hay live trading, no se
leen credenciales en el smoke y `models/latest_model.json` no se modifica
automaticamente.

Estado al 2026-06-17: se agrego el hito de campaña paper acumulada en
`paper-monitor`. El dashboard calcula siempre `stability` con umbral default de
60 sesiones paper completas (`READY` + ejecucion `SUBMITTED` + closeout
`CLOSED` + sin diagnostico/blocker asociado), reporta
`ready_for_live_review` como senal documental de revision manual futura y
mantiene `live_trading_authorized=false`. Tambien existe un snapshot Alpaca
paper read-only estrictamente opt-in (`--broker-read-only --confirm-paper`) que
consulta cuenta, posiciones allowlisted y ordenes abiertas, sin enviar, cerrar
ni cancelar ordenes. Si el snapshot falla por credenciales, dependencia o API,
el monitor escribe artefactos redacted con `status=ERROR` y retorna `2`.
Live trading sigue fuera de alcance.

Estado al 2026-06-18: se agregaron cierre diario paper, performance paper,
check operativo diario, rehearsal offline, indice de evidencia, validacion
formal de statement, decision humana de challenger y readiness/scaffold
read-only para futuros micro. `paper-day-close` produce
`reports/tmp/paper_decisions/<as_of_date>/decision.json` y `.md` con estados
`CONTINUE`, `REVIEW`, `STOP` o `ERROR`, hashes de artefactos y ledger redacted.
`paper-ops-check` consolida readiness, monitor, campaign, decision,
performance y ledgers antes del siguiente submit; `OK` exige readiness
`READY`, decision `CONTINUE`, performance presente y sin closeouts
pendientes/unmatched. `paper-statement-validate` normaliza CSV/JSON local del
broker, rechaza duplicados o campos obligatorios faltantes y produce
`statement.normalized.json` sin conectar broker.
`paper-performance-report` resume sesiones, submits, fills, closeouts
`PENDING`/`UNMATCHED`, warnings de precio, PnL `proxy` y brecha
paper-vs-backtest sin autorizar live; con statement validado marca PnL
`broker_statement`. `paper-weekly-summary --history-weeks 4` agrega
`blocker_aging` para recurrencia de blockers, dias consecutivos `REVIEW` y
ultimos `STOP`/`ERROR`. `evaluate-approved-data` ahora escribe
`walk_forward.json` y `regime_slices.json`, y la decision challenger incluye
costos, slippage, turnover, bloqueo por leakage temporal, robustez OOS,
drawdown y pocos trades. `paper-ops-rehearsal` genera fixtures locales para
ensayar statement, performance, ops check, weekly summary y decision humana sin
broker. `paper-evidence-index` resume los artefactos diarios/semanales y marca
faltantes opcionales como `WARN`. `model-review-decision` registra
`APPROVE_FOR_NEXT_PAPER_CYCLE`, `REJECT` o `DEFER` con hashes de artefactos y
nunca muta `models/latest_model.json`; `model-review-cycle-report` convierte
esa decision en recomendacion para el siguiente paper cycle sin promocion
automatica. `futures-readiness-report` valida fixtures MES/MNQ locales con
calendario, roll, tick size/value, margin placeholder, sesiones y costos.
`futures-research-scaffold` genera un manifest offline con contratos, tick
values, margin placeholders, sesiones, roll rules y data requirements; no lee
credenciales IBKR, no crea comandos de ejecucion futures y mantiene
`live_trading_allowed=false`.

Artefactos creados:

- `pyproject.toml`: paquete Python `trading-ai-research` con dependencias
  opcionales por grupos: research, ML, monitoring, forecasting, broker y
  notebook.
- `src/trading_ai/research/metrics.py`: metricas puras para retorno acumulado,
  max drawdown, Sharpe anualizado y sizing por volatilidad objetivo.
- `src/trading_ai/risk/policy.py`: risk gates deterministas que bloquean live
  trading por defecto y detectan brechas de perdida, drawdown y exposicion.
- `configs/universe.yml`: universo inicial de ETFs `SPY`, `QQQ`, `IWM`,
  `TLT`, `GLD`, `XLK`, `XLF`, `XLE`, `XLV` y `XLI`.
- `configs/risk.yml`: limites conservadores iniciales con perdida diaria
  maxima 2%, drawdown maximo 10%, exposicion bruta maxima 100%, peso maximo
  por activo 30% y live trading deshabilitado.
- `src/trading_ai/data/`: IO CSV/Parquet opcional, datos sample deterministas
  para smoke tests locales, validacion OHLCV, manifiestos reproducibles y hash
  SHA-256 canonico de datasets.
- `src/trading_ai/features/`: features de retornos, momentum, volatilidad
  realizada, drawdown rolling, medias moviles, rango diario y volumen relativo.
- `src/trading_ai/backtest/`: backtest reproducible de rotacion momentum con
  volatility targeting, costes, slippage, turnover y metricas obligatorias.
- `src/trading_ai/reports/`: reporte Markdown de backtest.
- `src/trading_ai/execution/alpaca_paper.py`: frontera Alpaca paper con dry-run
  por defecto, allowlist de simbolos, lectura normalizada de cuenta/posiciones,
  idempotencia por `client_order_id`, cancelacion idempotente, reconciliacion de
  posiciones, kill-switch y risk gate antes de aceptar ordenes.
- `src/trading_ai/execution/alpaca_connection.py`: helper opcional para crear
  un cliente `alpaca-py` en modo paper usando solo variables de entorno del
  proceso: `ALPACA_PAPER_API_KEY` y `ALPACA_PAPER_SECRET_KEY`.
- `src/trading_ai/execution/paper_daily.py` y `configs/paper_daily.yml`:
  operador diario paper-only y wrapper `paper-daily-from-readiness` para
  orquestar sesion offline, observability, monitor, submit Alpaca paper
  confirmado desde readiness aprobado, closeout y ledger redacted sin habilitar
  live trading. Un monitor `ERROR` antes del submit bloquea nuevas ordenes y
  sale como error operativo `2`.
- `src/trading_ai/execution/paper_monitor.py`: dashboard paper-only con
  estabilidad acumulada de 60 sesiones, `ready_for_live_review` documental,
  snapshot Alpaca paper read-only opcional y salida `ERROR` redacted para fallos
  operativos de broker.
- `src/trading_ai/evaluation/paper_daily_prepare.py`: readiness diaria que
  evalua y registra paquetes aprobados, genera config autocontenida y ejecuta
  smoke offline opt-in antes de cualquier corrida broker-confirmed.
- `src/trading_ai/llm/`: schemas Structured Outputs, wrapper OpenAI Responses
  API con `model="gpt-5.5"`, `store=False`, `reasoning.effort` configurable,
  logging JSONL de uso/latencia/errores, redaccion de claves tipo `sk-*`,
  evals locales de guardrails y sin permisos de ejecucion.
- `src/trading_ai/models/baseline.py`: baseline logistico puro Python para
  construir ejemplos supervisados sin fuga temporal, entrenar/evaluar una
  clasificacion direccional simple y registrar metricas temporales.
- `src/trading_ai/models/promotion.py`: gate champion/challenger que rechaza
  modelos sin suficiente evidencia out-of-sample o sin mejora minima frente al
  benchmark.
- `src/trading_ai/cli.py`: comandos `ingest`, `validate-data`, `manifest`,
  `build-features`, `backtest`, `train`, `evaluate`, `promote`, `llm-eval`,
  `report` y `paper`.
- `tests/`: pruebas unitarias stdlib para metricas y politica de riesgo.
- `configs/permissions.yml`: frontera de permisos para read-only, paper-only y
  live-prohibited.
- `docs/tooling-risk-register.md`: matriz inicial de riesgos para dependencias,
  modelos, MCP, paper trading y broker live.
- `docs/model-evaluation-policy.md`: gates minimos de evaluacion, walk-forward,
  champion/challenger, paper trading y live trading.
- `notebooks/benchmark_baseline_vs_ml_vs_timeseries.ipynb`: notebook de
  benchmark con datos sinteticos, baseline determinista, hooks para ML y
  forecasting, y validacion de risk gates.
- `docs/hardware-model-sizing.md`: revision del hardware local y decision de
  que modelos conviene correr localmente frente a API o servidor GPU.
- `docs/superpowers/plans/2026-06-16-trading-ai-research-mvp.md`: trazabilidad
  del plan ejecutado.

Verificacion local realizada:

La verificacion oficial actual es `unittest` de stdlib. `ruff`, `mypy` y
`pre-commit` no son gates configurados para este repo por ahora.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m json.tool notebooks/benchmark_baseline_vs_ml_vs_timeseries.ipynb >/dev/null
PYTHONPATH=src python3 -m trading_ai.cli ingest --config configs/universe.yml --from 2024-01-01 --to 2024-03-31 --output /tmp/trading_ai_etfs.csv
PYTHONPATH=src python3 -m trading_ai.cli validate-data --dataset /tmp/trading_ai_etfs.csv
PYTHONPATH=src python3 -m trading_ai.cli manifest --dataset /tmp/trading_ai_etfs.csv --output /tmp/trading_ai_etfs.manifest.json
PYTHONPATH=src python3 -m trading_ai.cli build-features --dataset /tmp/trading_ai_etfs.csv --output /tmp/trading_ai_features.csv
PYTHONPATH=src python3 -m trading_ai.cli backtest --strategy momentum-vol-target --config configs/risk.yml --dataset /tmp/trading_ai_etfs.csv --output /tmp/trading_ai_backtest.json --report-output /tmp/trading_ai_backtest.md
PYTHONPATH=src python3 -m trading_ai.cli train --model logistic-baseline --dataset /tmp/trading_ai_features.csv --output /tmp/trading_ai_model.json --run-output /tmp/trading_ai_model_run.json
PYTHONPATH=src python3 -m trading_ai.cli evaluate --run-id /tmp/trading_ai_model_run.json --output /tmp/trading_ai_model_eval.json
PYTHONPATH=src python3 -m trading_ai.cli promote --run-id /tmp/trading_ai_model_run.json --baseline /tmp/baseline_classifier_metrics.json --output /tmp/trading_ai_promotion.json
PYTHONPATH=src python3 -m trading_ai.cli llm-eval --output /tmp/trading_ai_llm_guardrail_eval.json
PYTHONPATH=src python3 -m trading_ai.cli report --run-id /tmp/trading_ai_backtest.json --output /tmp/trading_ai_report.md
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --universe configs/universe.yml --risk configs/risk.yml
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --universe configs/universe.yml --risk configs/risk.yml --kill-switch-test --output /tmp/trading_ai_paper_kill_switch.json
PYTHONPATH=src python3 -m trading_ai.cli paper --broker alpaca --dry-run --read-account --output /tmp/trading_ai_paper_status.json
```

Verificacion adicional del hito diario paper-only al 2026-06-17:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_prepare_paper_daily tests.test_paper_daily -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_paper_monitor tests.test_paper_observability -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Resultado local: las suites enfocadas de paper monitor/observability pasaron;
la suite completa paso con skips esperados por extras `pandas`/`pyarrow` no
instalados.

Tambien se compilaron y ejecutaron las celdas ligeras del notebook con Python
stdlib. El notebook no demuestra rentabilidad; solo valida que el scaffold
calcula metricas, aplica risk gates y mantiene live trading bloqueado.

Nota de datos: el comando `ingest` incluye datos sample deterministas para
smoke tests sin red. Para investigacion real, usar `--source-csv` con datos
descargados desde un proveedor aprobado y versionarlos en `data/raw` o activar
Parquet instalando `pandas` y `pyarrow` en un entorno Python 3.12.

Nota de modelos: el baseline logistico es solo infraestructura de evaluacion
temporal y reproducibilidad. LightGBM/XGBoost, Optuna, MLflow y Chronos siguen
pendientes hasta instalar dependencias en Python 3.12 y sustituir los datos
sample por datos de mercado aprobados.

Nota de promocion: una decision aprobada por `promote` significa elegibilidad
como challenger para paper review. No reemplaza al champion automaticamente, no
autoriza ordenes y no cambia limites de riesgo.

Nota LLM: `llm-eval` solo ejecuta evals locales de guardrails y no llama a la
API de OpenAI. Las llamadas reales al wrapper deben inyectar o construir un
cliente OpenAI con credenciales fuera del LLM; el wrapper no lee `.env`
directamente, usa `store=False`, Structured Outputs via `text.format` y registra
uso/latencia/errores cuando recibe `usage_log_path`.

Nota paper execution: la Fase 5 ya tiene una frontera local/mocked para Alpaca
paper con pruebas de lectura de cuenta, lectura de posiciones allowlisted,
cancelacion idempotente, reconciliacion y kill-switch. El comando
`paper --kill-switch-test` genera un JSON reproducible; la corrida local actual
dejo el snapshot historico
`reports/historical/legacy-smoke/paper_kill_switch_test.json`, mientras que los
nuevos defaults escriben en `reports/tmp/paper/latest.json`. Tambien existe el puente read-only a
una cuenta Alpaca paper real mediante
`paper --real-paper --confirm-paper --read-account --read-positions`, usando
`alpaca-py` como dependencia opcional y variables de entorno del proceso. El
comando `paper-execute-session --session-dir <dir> --confirm-paper
--confirm-submit` ya permite ejecutar una orden Alpaca paper desde una sesion
offline aprobada, pero sigue siendo un paso manual, confirmado, paper-only y sin
live trading; debe seguir el runbook `docs/paper-real-runbook.md`.

La implementacion no codifica supuestos de mercado cambiantes como verdades
operativas. Las decisiones sensibles a fecha, disponibilidad de producto,
licencias, permisos de broker, APIs y modelos deben volver a verificarse contra
fuentes primarias antes de instalar dependencias, conectar cuentas paper o
descargar datos de mercado.

## 16. Fuentes verificadas

- CME FX futures and options: https://www.cmegroup.com/markets/fx.html
- CME equity index futures: https://www.cmegroup.com/markets/equities.html
- QuantConnect LEAN GitHub: https://github.com/QuantConnect/Lean
- QuantConnect LEAN CLI docs: https://www.quantconnect.com/docs/v2/lean-cli
- QuantConnect Interactive Brokers integration: https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/interactive-brokers
- Interactive Brokers TWS API docs: https://interactivebrokers.github.io/tws-api/introduction.html
- Alpaca Trading API: https://docs.alpaca.markets/us/docs/trading-api
- NinjaTrader developer docs: https://developer.ninjatrader.com/docs/desktop
- OANDA v20 REST API: https://developer.oanda.com/rest-live-v20/introduction/
- Coinbase Derivatives: https://www.coinbase.com/derivatives
- Coinbase Derivatives API docs: https://docs.cdp.coinbase.com/derivatives/introduction/welcome
- CFTC FCM registration overview: https://www.cftc.gov/IndustryOversight/Intermediaries/FCMs/index.htm
- NFA BASIC: https://www.nfa.futures.org/basicnet/
- NautilusTrader docs: https://nautilustrader.io/docs/latest/
- NautilusTrader GitHub: https://github.com/nautechsystems/nautilus_trader
- FreqAI docs: https://www.freqtrade.io/en/stable/freqai/
- Freqtrade leverage/futures docs: https://www.freqtrade.io/en/stable/leverage/
- Hummingbot strategies docs: https://hummingbot.org/strategies/
- Databento docs: https://databento.com/docs
- MCP introduction: https://modelcontextprotocol.io/docs/getting-started/intro
- MCP reference servers: https://github.com/modelcontextprotocol/servers
- Qwen3 model card: https://huggingface.co/Qwen/Qwen3-32B
- Qwen3-1.7B model card: https://huggingface.co/Qwen/Qwen3-1.7B
- Qwen3-4B model card: https://huggingface.co/Qwen/Qwen3-4B
- Qwen3 Embedding 0.6B model card: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- DeepSeek-R1 GitHub: https://github.com/deepseek-ai/DeepSeek-R1
- Llama 4 models: https://www.llama.com/models/llama-4/
- Gemma 4 model overview: https://ai.google.dev/gemma/docs/core
- Gemma 3 model card: https://ai.google.dev/gemma/docs/core/model_card_3
- SmolLM3-3B model card: https://huggingface.co/HuggingFaceTB/SmolLM3-3B
- Phi-4-mini-instruct model card: https://huggingface.co/microsoft/Phi-4-mini-instruct
- IBM Granite 3.3 2B Instruct model card: https://huggingface.co/ibm-granite/granite-3.3-2b-instruct
- Mistral 7B Instruct model card: https://huggingface.co/mistralai/Mistral-7B-Instruct-v0.3
- ProsusAI FinBERT GitHub: https://github.com/ProsusAI/finBERT
- ProsusAI FinBERT model card: https://huggingface.co/ProsusAI/finbert
- BGE small embedding model card: https://huggingface.co/BAAI/bge-small-en-v1.5
- all-MiniLM-L6-v2 model card: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
- TimesFM GitHub: https://github.com/google-research/timesfm
- Chronos forecasting GitHub: https://github.com/amazon-science/chronos-forecasting
- Chronos-Bolt small model card: https://huggingface.co/amazon/chronos-bolt-small
- MLflow docs: https://mlflow.org/docs/latest/index.html
- Evidently docs: https://docs.evidentlyai.com/introduction
- Optuna docs: https://optuna.readthedocs.io/en/stable/
- Ray RLlib docs: https://docs.ray.io/en/latest/rllib/index.html
- Darts docs: https://unit8co.github.io/darts/
- FinGPT GitHub: https://github.com/AI4Finance-Foundation/FinGPT
- FinRobot GitHub: https://github.com/AI4Finance-Foundation/FinRobot
- FinRL-Trading / FinRL-X GitHub: https://github.com/AI4Finance-Foundation/FinRL-Trading
- OpenBB GitHub: https://github.com/OpenBB-finance/OpenBB
- LiveTradeBench paper: https://arxiv.org/abs/2511.03628
- QuantCode-Bench paper: https://arxiv.org/abs/2604.15151
- Research on SKILL.md supply-chain attacks: https://arxiv.org/abs/2605.11418
- Research on coding-agent skill supply-chain attacks: https://arxiv.org/abs/2604.03081
- Backtest overfitting paper: https://arxiv.org/abs/1408.1159
- Deflated Sharpe Ratio reference: https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio
- Purged cross-validation reference: https://en.wikipedia.org/wiki/Purged_cross-validation
