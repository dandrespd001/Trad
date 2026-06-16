# Revision de hardware y dimensionamiento de modelos locales

Fecha: 2026-06-16

## 1. Resumen ejecutivo

El equipo sirve bien para **research cuantitativo local**, backtests, notebooks,
modelos tabulares, embeddings pequenos, sentiment financiero y forecasting
ligero. Tambien puede correr LLMs pequenos localmente, pero no es una maquina
adecuada para LLMs grandes, inferencia concurrente, contextos largos o
fine-tuning serio.

Recomendacion practica:

1. **Usar local para Fase 1-2:** ingestion, features, backtesting,
   LightGBM/XGBoost/scikit-learn, MLflow local, Evidently, FinBERT,
   embeddings pequenos y Chronos-Bolt tiny/mini/small.
2. **Usar local con cautela para LLM:** Qwen3-1.7B, Granite 3.3 2B y Qwen3-4B
   cuantizados. Qwen3-4B debe tratarse como limite superior razonable, no como
   modelo principal de produccion.
3. **Pagar API o servidor GPU para tareas pesadas:** modelos 7B+ responsivos,
   14B/32B, vLLM/SGLang, contextos largos, multiples usuarios, RAG grande o
   LoRA/QLoRA de LLMs.
4. **No usar LLM local como autoridad de ordenes:** para el trading bot, el LLM
   debe analizar documentos, noticias, logs y explicaciones. Las ordenes siguen
   pasando por motor determinista y risk gates.

## 2. Hardware detectado

| Componente | Resultado |
| --- | --- |
| CPU | Intel Core i7-10700T, 8 cores / 16 threads, hasta 4.5 GHz |
| Instrucciones CPU | AVX2, FMA, SSE4.x, AES |
| RAM | 31 GiB total, ~21 GiB disponible durante la medicion |
| Swap | 31 GiB zram |
| GPU | NVIDIA GeForce GTX 1650 |
| VRAM | 4096 MiB total, ~3319 MiB libre fuera del sandbox |
| Compute capability | 7.5 |
| Driver | NVIDIA 610.43.02 |
| CUDA UMD | 13.3 |
| Disco principal | NVMe Samsung 512 GB, ~419 GB libres |
| Disco secundario | HDD 1 TB NTFS |
| Sistema | CachyOS Linux, kernel 7.0.12-1-cachyos |
| Docker | Instalado: 29.5.2 |
| Ollama | No instalado |
| CUDA toolkit `nvcc` | No instalado |
| Python sistema | 3.14.5 |

Nota: dentro del sandbox de Codex, `nvidia-smi` no ve `/dev/nvidia*`; fuera del
sandbox si confirma la GPU. Para trabajos reales de GPU hay que ejecutarlos en
el host o en contenedores con acceso explicito a NVIDIA.

## 3. Implicaciones tecnicas

### CPU y RAM

El CPU es suficiente para:

- backtesting diario/horario;
- pipelines pandas/polars;
- scikit-learn, LightGBM, XGBoost y statsmodels;
- validaciones walk-forward no masivas;
- inferencia CPU de modelos pequenos o cuantizados;
- embeddings pequenos y FinBERT.

La RAM de 32 GB permite cargar datasets medianos y modelos cuantizados de 7B en
CPU, pero eso no significa que la experiencia sea buena. Un 7B cuantizado puede
correr, pero sera lento y no conviene para iteracion frecuente.

### GPU

La GTX 1650 tiene solo 4 GB de VRAM. Eso sirve para acelerar modelos pequenos,
pero no para alojar comodos LLMs grandes.

Uso razonable de GPU:

- LLM 1B-2B cuantizado;
- Qwen3-4B cuantizado con contexto corto y expectativas moderadas;
- FinBERT y embeddings;
- Chronos-Bolt small/base;
- entrenamiento ligero de modelos pequenos.

Uso no recomendado en esta GPU:

- LLM 7B+ completamente en VRAM;
- contextos largos con Qwen3-4B;
- vLLM/SGLang de produccion;
- multiples usuarios;
- fine-tuning LoRA/QLoRA de 4B+ sin mucho ajuste y paciencia;
- modelos 14B/32B.

### Python

El Python del sistema es 3.14.5. Muchas librerias de ML, CUDA y notebooks suelen
ir por detras de la ultima version de Python. Para este proyecto conviene crear
un entorno aislado con **Python 3.11 o 3.12** para evitar incompatibilidades.

## 4. Modelos recomendados por rol

| Rol | Local recomendado | Local limite | Mejor en servidor/API |
| --- | --- | --- | --- |
| LLM generativo/RAG | Granite 3.3 2B, Qwen3-1.7B cuantizado | Qwen3-4B cuantizado | Qwen3 8B/14B/32B, DeepSeek/R1 distill grande, modelos propietarios |
| Explicacion de logs/trades | Granite 2B, Qwen3-1.7B | Qwen3-4B | 7B+ si se necesita razonamiento mas fuerte |
| Embeddings/RAG | all-MiniLM, BGE-small, Qwen3-Embedding-0.6B | reranker pequeno | embeddings grandes o rerankers pesados |
| Sentiment financiero | FinBERT/finbert-tone | LLM pequeno con LoRA | modelos financieros grandes o APIs |
| Forecasting | Chronos-Bolt tiny/mini/small | Chronos-Bolt base, TimesFM pequeno | TimesFM/Chronos grandes, ensembles pesados |
| ML tabular | scikit-learn, LightGBM, XGBoost | Optuna limitado | busqueda masiva de hiperparametros |
| RL/deep learning | no recomendado para MVP | experimentos muy pequenos | servidor GPU con simulador realista |

## 5. Seleccion concreta para el trading bot

### Fase local recomendada

Usar este equipo para:

1. Backtesting reproducible con datos diarios/1h.
2. Baseline momentum + volatility targeting.
3. Modelos tabulares: LightGBM/XGBoost.
4. Sentiment offline: FinBERT.
5. RAG local pequeno: BGE-small o Qwen3-Embedding-0.6B.
6. LLM local pequeno: Granite 3.3 2B o Qwen3-1.7B.
7. Forecasting challenger: Chronos-Bolt small.

### No invertir tiempo local en

- entrenar LLMs desde cero;
- servir 7B+ para uso interactivo serio;
- correr 14B/32B;
- hacer fine-tuning LLM pesado;
- usar el LLM como trader autonomo;
- optimizar miles de trials con Optuna en intradia.

## 6. Cuando conviene pagar o usar servidor

Conviene pagar API o alquilar servidor GPU si se necesita cualquiera de estos
casos:

- respuestas LLM rapidas y de buena calidad;
- modelo 7B+ con contexto util;
- analisis de muchos documentos/noticias/reportes por dia;
- RAG con corpus grande y reranking pesado;
- varias sesiones concurrentes;
- fine-tuning LoRA/QLoRA;
- vLLM/SGLang/OpenAI-compatible endpoint;
- backtests intradia/tick con datasets grandes;
- entrenamiento recurrente de modelos deep learning.

Dimensionamiento recomendado de servidor:

| Objetivo | GPU sugerida |
| --- | --- |
| 7B/8B cuantizado con buena respuesta | 12-16 GB VRAM minimo |
| 7B/8B FP16 o 14B cuantizado comodo | 24 GB VRAM |
| 14B/32B, contexto largo, varios usuarios | 48 GB+ VRAM |
| fine-tuning serio o serving continuo | 48-80 GB VRAM segun modelo |

Para este proyecto, la opcion mas eficiente suele ser hibrida:

- **local:** datos, features, backtests, modelos tabulares, risk gates y modelos
  pequenos;
- **API/servidor:** LLM fuerte para investigacion, resumen, razonamiento,
  generacion de hipotesis y analisis de reportes.

## 7. Proxima prueba tecnica recomendada

1. Crear entorno Python 3.11/3.12.
2. Instalar una runtime local: `llama.cpp`, LM Studio u Ollama.
3. Probar tres modelos cuantizados:
   - Granite 3.3 2B Instruct Q4/Q5;
   - Qwen3-1.7B Q4/Q5;
   - Qwen3-4B Q4, solo si el contexto se mantiene corto.
4. Medir:
   - tokens/segundo;
   - RAM y VRAM;
   - latencia primer token;
   - calidad en tareas reales: resumen de logs, explicacion de backtest,
     extraccion de eventos financieros.
5. Comparar contra una API externa o servidor GPU durante una semana de uso
   real antes de comprometer arquitectura.

## 8. Fuentes tecnicas consultadas

- Qwen3-4B model card: https://huggingface.co/Qwen/Qwen3-4B
- Qwen3-1.7B model card: https://huggingface.co/Qwen/Qwen3-1.7B
- IBM Granite 3.3 2B Instruct model card: https://huggingface.co/ibm-granite/granite-3.3-2b-instruct
- Chronos-Bolt small model card: https://huggingface.co/amazon/chronos-bolt-small
