# Revision de skills/plugins de terceros y modelos IA open source para el trading bot

Fecha: 2026-06-16

## 1. Resultado ejecutivo

No recomiendo instalar cualquier skill/plugin de terceros solo porque prometa "trading", "AI agent" o "broker automation". En este proyecto, una skill o MCP maliciosa o mal disenada podria leer credenciales, alterar codigo, filtrar datos o incluso enviar ordenes si se le da acceso. La regla debe ser:

- instalar solo desde repositorios confiables;
- fijar commit/tag, no ramas flotantes;
- revisar `SKILL.md`, scripts, dependencias y permisos;
- usar cuentas paper/read-only por defecto;
- nunca dar permisos de trading live a un LLM o MCP sin un risk gate determinista;
- separar research, paper trading y live trading en entornos distintos.

La recomendacion tecnica queda asi:

1. **Mantener las skills Codex oficiales ya instaladas** como base: `openai-docs`, `jupyter-notebook`, `cli-creator`, `security-*`, `sentry`, `playwright`, `pdf`.
2. **Agregar herramientas open source dentro del proyecto**, no como skills globales de Codex, para modelos y MLOps: `MLflow`, `Evidently`, `Optuna`, `DVC` o versionado Parquet, `TimesFM`, `Chronos`, `LightGBM`, `PyTorch`, `scikit-learn`.
3. **Usar modelos LLM open-weight solo para analisis, RAG, sentiment, resumen y explicacion**, no para enviar ordenes directamente.
4. **Usar modelos de series temporales como challengers**, no como fuente unica de senales. Deben competir contra baselines simples.
5. **Instalar skills/MCP de terceros solo tras auditoria**, empezando por MCP de lectura: `filesystem`, `git`, `fetch`, `postgres/sqlite` read-only, `memory` controlado. Evitar MCP de broker con permisos de orden hasta tener sandbox y kill-switch.

## 2. Skills y plugins Codex

### Estado local

Ya estan instaladas o disponibles skills suficientes para avanzar con buena calidad:

| Skill/capacidad | Estado | Valor para el proyecto |
| --- | --- | --- |
| `openai-docs` | instalada | Diseno LLM, agentes, evaluaciones, APIs y structured outputs. |
| `jupyter-notebook` | instalada | Experimentos, notebooks de research, backtesting y entrenamiento. |
| `cli-creator` | instalada | CLI reproducible para `ingest`, `train`, `backtest`, `paper`, `eval`, `deploy`. |
| `security-best-practices` | instalada | Revision de codigo sensible. |
| `security-threat-model` | instalada | Threat model de credenciales, broker, datos y modelos. |
| `security-ownership-map` | instalada | Ownership/riesgo si crece el equipo. |
| `sentry` | instalada | Observabilidad y errores runtime. |
| `playwright` | instalada | Pruebas de dashboards o interfaces web. |
| `pdf` | instalada | Reportes de investigacion/performance. |
| `define-goal` | instalada | Seguimiento de objetivos largos. |

No encontre en el catalogo oficial una skill especifica y madura para trading cuantitativo. Eso es razonable: para este proyecto conviene construir las herramientas de trading dentro del repo, auditables y testeables, no delegarlas a una skill opaca.

### Candidatos de terceros

| Candidato | Tipo | Recomendacion |
| --- | --- | --- |
| TimesFM `SKILL.md` en `google-research/timesfm` | Skill/agent instructions de forecasting | Candidato interesante para research de series temporales. No instalar globalmente sin revisar el archivo y fijar commit. |
| MCP reference servers oficiales | MCP | Usar solo los necesarios. `filesystem`, `git`, `fetch`, `memory`, `time` son utiles; `postgres/sqlite` solo read-only. |
| MCP de broker/trading | MCP | Evitar al inicio. Si se usa, debe ser paper-only, con allowlist de simbolos, limites de tamano y sin credenciales live. |
| Skills de "trading bot" encontradas en GitHub | Skill third-party | No recomendadas sin auditoria profunda. Alto riesgo de instrucciones maliciosas o malas practicas. |
| Plugins UI/dashboard | Plugin/app | Utiles para monitoreo, pero deben conectarse a datos ya saneados, no directamente al broker live. |

## 3. Riesgo de supply chain en skills/MCP

Las skills no son solo documentacion: sus instrucciones afectan como el agente elige herramientas y que acciones ejecuta. Por eso, antes de instalar terceros:

1. Revisar `SKILL.md` completo.
2. Revisar scripts y comandos que propone.
3. Buscar instrucciones de exfiltracion, secretos, bypass de permisos o auto-instalacion.
4. Revisar dependencias y lockfiles.
5. Pinnear a commit/tag.
6. Instalar en entorno aislado primero.
7. Ejecutar con permisos minimos.

Para este trading bot, las skills/MCP con mayor riesgo son:

- broker/trading/order execution;
- filesystem con acceso amplio;
- shell/terminal sin restricciones;
- browser automation con sesiones autenticadas;
- database write;
- secrets managers;
- cualquier tool que combine lectura de secretos + red externa.

## 4. Modelos LLM open source/open-weight recomendados

La distincion importa:

- **Open source real:** codigo, pesos y licencia permisiva suficientemente clara.
- **Open-weight:** pesos disponibles, pero datos/licencia pueden ser restrictivos.
- **Source-available:** acceso util, pero no necesariamente libre para uso comercial.

| Modelo/familia | Licencia/estado | Uso recomendado | Comentario |
| --- | --- | --- | --- |
| Qwen3 | Apache 2.0 en modelos como `Qwen3-32B` | RAG, analisis, tool-use local, razonamiento general | Muy buen candidato local si hay GPU suficiente; soporta modo thinking/no-thinking y tool use. |
| DeepSeek-R1 / distilled | MIT segun repo oficial | Razonamiento offline, generacion de hipotesis, analisis complejo | Bueno para tareas de razonamiento; revisar riesgos de safety y sesgos. |
| SmolLM3-3B | Apache 2.0 | LLM pequeno para RAG, resumen, explicacion y analisis en espanol/ingles | Buen candidato si se prioriza entrenamiento barato y modelo abierto. |
| Phi-4-mini-instruct | MIT | Razonamiento pequeno, RAG y tool-use | Buen candidato de 3.8B; requiere RAG para reducir errores factuales. |
| IBM Granite 3.3 2B Instruct | Apache 2.0 | RAG, extraccion, clasificacion y function calling | Muy buen candidato pequeno con licencia permisiva. |
| Llama 4 Scout/Maverick | licencia Meta | Analisis multimodal y texto largo | No es licencia OSI; util si los terminos encajan. Scout destaca por contexto largo. |
| Gemma 4 E2B/E4B o Gemma 3 1B/4B | Gemma license | Modelos pequenos/edge, analisis local y prototipos | Revisar terminos; Gemma 4 agrega modelos pequenos E2B/E4B, contexto grande y QAT. |
| Mistral/Mixtral abiertos | Apache 2.0 en modelos historicos como Mistral 7B, Mixtral | Baseline local, embeddings/analisis, tareas generales | Buen ecosistema, pero revisar licencia de cada modelo nuevo; no todos son Apache/comerciales. |
| FinGPT | MIT repo; modelos financieros en HF | Sentiment financiero, headlines, NER, analisis de noticias | Util como componente especializado, no como trader autonomo. |
| FinRobot | Apache 2.0 | Research financiero, reportes, agentes de analisis | Bueno para research cualitativo/equity reports; no usar como motor live. |

Recomendacion:

- **Primer LLM local pequeno:** Qwen3-4B o Granite 2B; Qwen3-1.7B/SmolLM3-3B si el hardware es limitado; Qwen3 14B/32B solo como comparador mas capaz.
- **Razonamiento offline:** DeepSeek-R1 distilled o Qwen3 thinking.
- **Finanzas/texto:** FinGPT como baseline de sentiment y extraccion, comparado contra modelos generales.
- **No entrenar un LLM financiero desde cero.** Usar LoRA/QLoRA sobre modelos existentes y evaluar contra tareas concretas.

### Modelos pequenos para fine-tuning local

Para especializar el bot sin depender de modelos grandes, la pila recomendada es:

| Rol | Modelo inicial | Alternativas | Entrenamiento |
| --- | --- | --- | --- |
| Generativo/RAG | Qwen3-4B | Qwen3-1.7B, SmolLM3-3B, Granite 2B, Phi-4-mini, Gemma 4 E2B/E4B | LoRA/QLoRA con instrucciones propias, logs, explicaciones y resumen de reportes. |
| Sentiment financiero | FinBERT/finbert-tone | LLM pequeno con LoRA | Fine-tuning supervisado con etiquetas bullish/bearish/neutral, riesgo y tipo de evento. |
| Embeddings/RAG | Qwen3-Embedding-0.6B | BGE-small, all-MiniLM-L6-v2 | Fine-tuning contrastivo con pares pregunta-documento, noticia-evento y log-incidente. |
| Forecasting | Chronos-Bolt small | Chronos-2 small, TimesFM 2.5 | Benchmark walk-forward; usar como challenger, no como fuente unica de senal. |

Orden recomendado:

1. Entrenar primero FinBERT/embeddings y modelos tabulares.
2. Ajustar un LLM pequeno solo cuando existan datasets y evals propios.
3. Registrar cada adaptador LoRA y dataset en MLflow.
4. Promover solo por champion/challenger y con impacto medible en backtest/paper.

## 5. Modelos de series temporales y forecasting

Para trading, los modelos de series temporales son mas relevantes que un LLM general cuando la tarea es predecir retorno, volatilidad, rango, drawdown o probabilidad de breakout. Aun asi, no deben usarse sin costos, slippage y validacion walk-forward.

| Modelo/herramienta | Uso recomendado | Veredicto |
| --- | --- | --- |
| TimesFM 2.5 | Forecasting zero-shot/fine-tuning de series; LoRA/PEFT disponible | Candidato fuerte para research. Probar como challenger contra baselines simples. |
| Chronos-2 | Forecasting univariado, multivariado y con covariables | Candidato fuerte para senales de precio/volatilidad con variables exogenas. |
| NeuralForecast | Modelos deep learning de forecast en Python | Bueno para research estructurado y benchmarks. |
| Darts | Forecasting, regresion, clasificacion y anomaly detection | Bueno para prototipado y comparacion amplia de modelos. |
| LightGBM/XGBoost/CatBoost | ML tabular con features financieras | Deben ser los primeros modelos productivos por interpretabilidad, velocidad y robustez. |
| RLlib / Stable-Baselines3 | Reinforcement learning | No usar al inicio en vivo. Solo despues de tener simulador realista y offline RL controlado. |

Recomendacion:

1. Baseline: momentum + volatility targeting.
2. ML tabular: LightGBM/XGBoost con features de precio, volatilidad, regimen, calendario y macro.
3. Foundation forecasting: TimesFM/Chronos como challengers.
4. LLM/FinGPT: sentiment/news como feature secundaria.
5. RL: fase experimental, nunca primer motor live.

## 6. MLOps, auditoria y mejora continua

| Herramienta | Uso | Recomendacion |
| --- | --- | --- |
| MLflow | Tracking, model registry, evaluacion, LLM/agent observability | Instalar en el proyecto. Muy util desde fase 1. |
| Evidently | Data drift, ML monitoring, LLM eval, dashboards | Instalar en fase 1-2 para drift y calidad. |
| Optuna | Hyperparameter tuning con pruning y estudios reproducibles | Usar con limites estrictos para evitar overfitting. |
| DVC | Versionado de datasets/modelos | Candidato si los datos crecen; al inicio puede bastar Parquet + hashes. |
| Great Expectations / pandera | Validacion de datos | Recomendado para ingestion y features. |
| Sentry | Errores runtime | Ya tenemos skill; integrar SDK cuando haya servicios. |
| Prometheus/Grafana | Metricas operativas | Fase 2 si hay workers/live paper. |

## 7. Herramientas financieras open source

| Herramienta | Uso | Veredicto |
| --- | --- | --- |
| QuantConnect LEAN | Motor multi-activo backtest/live | Backbone recomendado para multi-activo. |
| NautilusTrader | Motor avanzado event-driven | Excelente para arquitectura seria, pero curva alta. |
| OpenBB | Plataforma de datos financieros para analysts/quants/AI agents | Muy util como data/research layer, revisar licencia AGPLv3 y proveedores. |
| FinRL-X / FinRL-Trading | Arquitectura AI-native, paper con Alpaca, research | Fuente de ideas y prototipos; no reemplaza hardening propio. |
| Freqtrade/FreqAI | Cripto + ML | Solo si cripto se vuelve prioridad. |
| Hummingbot | Market making y conectores cripto | No prioritario para acciones/futuros. |

## 8. Recomendacion concreta de instalacion/proyecto

No instalaria mas skills globales todavia. En cambio, agregaria dependencias dentro del repo en fases:

### Fase A: research reproducible

- `pandas`/`polars`
- `pyarrow`
- `scikit-learn`
- `lightgbm`
- `xgboost`
- `statsmodels`
- `mlflow`
- `optuna`
- `evidently`
- `pandera`
- `jupyter`

### Fase B: forecasting/modelos IA

- `torch`
- `transformers`
- `accelerate`
- `peft`
- `bitsandbytes` si hay GPU compatible
- `timesfm`
- `chronos-forecasting`
- `neuralforecast` o `darts`

### Fase C: serving y agentes

- `vllm` o `llama.cpp`/`ollama` para inferencia local segun hardware
- API local compatible OpenAI para LLM interno
- RAG con `qdrant`, `chromadb` o `pgvector`
- MCP solo para lectura/auditoria, no para broker live

### Fase D: trading/paper

- LEAN CLI / Docker
- IBKR paper o Alpaca paper
- risk engine propio
- dashboard y logs
- Sentry/Prometheus segun despliegue

## 9. Lo que evitaria

- Instalar una skill de GitHub que prometa "make money trading bot" sin auditoria.
- Dar a un MCP acceso directo a `.env`, broker live y red externa a la vez.
- Usar un LLM como decision maker final de ordenes.
- Entrenar un modelo con datos recientes y evaluarlo en el mismo periodo.
- Optimizar parametros con Optuna sin embargo/purging/walk-forward.
- Usar yfinance o datos gratuitos como fuente final para trading intradia/futuros.
- Tomar resultados de FinRL/FinGPT/FinRobot como evidencia de rentabilidad sin reproducirlos.

## 10. Siguiente paso recomendado

Crear un entorno de proyecto con:

1. `pyproject.toml` con dependencias de Fase A.
2. `notebooks/` para research.
3. `src/` con ingestion, features, backtesting, model training y risk gates.
4. MLflow local.
5. Evidently para drift/quality reports.
6. Un notebook de benchmark: baseline vs LightGBM vs TimesFM/Chronos.

Despues de eso, si se quiere instalar una skill externa concreta, recomiendo empezar por auditar el `SKILL.md` de TimesFM y solo instalarla desde un commit fijo si aporta flujo real al trabajo.

## 11. Fuentes

- Official Codex skills catalog via local `skill-installer`.
- MCP introduction: https://modelcontextprotocol.io/docs/getting-started/intro
- MCP reference servers: https://github.com/modelcontextprotocol/servers
- Qwen3 model card: https://huggingface.co/Qwen/Qwen3-32B
- DeepSeek-R1 GitHub: https://github.com/deepseek-ai/DeepSeek-R1
- Llama 4 models: https://www.llama.com/models/llama-4/
- Gemma 4 model overview: https://ai.google.dev/gemma/docs/core
- SmolLM3-3B model card: https://huggingface.co/HuggingFaceTB/SmolLM3-3B
- Phi-4-mini-instruct model card: https://huggingface.co/microsoft/Phi-4-mini-instruct
- IBM Granite 3.3 2B Instruct model card: https://huggingface.co/ibm-granite/granite-3.3-2b-instruct
- Qwen3-1.7B model card: https://huggingface.co/Qwen/Qwen3-1.7B
- Qwen3-4B model card: https://huggingface.co/Qwen/Qwen3-4B
- Qwen3 Embedding 0.6B model card: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- ProsusAI FinBERT GitHub: https://github.com/ProsusAI/finBERT
- BGE small embedding model card: https://huggingface.co/BAAI/bge-small-en-v1.5
- all-MiniLM-L6-v2 model card: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
- Chronos-Bolt small model card: https://huggingface.co/amazon/chronos-bolt-small
- TimesFM GitHub: https://github.com/google-research/timesfm
- Chronos forecasting GitHub: https://github.com/amazon-science/chronos-forecasting
- MLflow docs: https://mlflow.org/docs/latest/index.html
- Evidently docs: https://docs.evidentlyai.com/introduction
- Optuna docs: https://optuna.readthedocs.io/en/stable/
- Ray RLlib docs: https://docs.ray.io/en/latest/rllib/index.html
- Darts docs: https://unit8co.github.io/darts/
- FinGPT GitHub: https://github.com/AI4Finance-Foundation/FinGPT
- FinRobot GitHub: https://github.com/AI4Finance-Foundation/FinRobot
- FinRL-Trading GitHub: https://github.com/AI4Finance-Foundation/FinRL-Trading
- OpenBB GitHub: https://github.com/OpenBB-finance/OpenBB
- Research on skill supply-chain attacks: https://arxiv.org/abs/2605.11418
