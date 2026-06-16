# Capacidades Codex instaladas para el proyecto de trading bot

Fecha: 2026-06-16

## Skills instaladas ahora

| Skill | Uso en este proyecto |
| --- | --- |
| `openai-docs` | Consultar documentacion oficial actualizada de OpenAI para disenar agentes, modelos, prompts, evaluaciones, APIs y patrones de integracion LLM. |
| `define-goal` | Mantener objetivos largos con presupuesto y estado, util para fases de investigacion, implementacion, auditoria y entrenamiento continuo. |
| `sentry` | Preparar observabilidad de errores/runtime para el bot, APIs, workers, pipelines de datos y servicios de inferencia. |
| `notion-research-documentation` | Convertir investigacion y decisiones tecnicas en documentacion estructurada si se usa Notion como base de conocimiento. |

## Skills ya disponibles antes

| Skill | Uso en este proyecto |
| --- | --- |
| `jupyter-notebook` | Crear notebooks de investigacion, backtesting, exploracion de datos y experimentos de modelos. |
| `cli-creator` | Crear CLIs internas para ingestion, backtest, entrenamiento, evaluacion, despliegue y control del bot. |
| `playwright` / `playwright-interactive` | Automatizar pruebas de interfaces web, dashboards, paneles de broker o herramientas internas. |
| `pdf` | Leer/generar reportes PDF de research, auditoria, performance o documentacion de brokers. |
| `security-best-practices` | Revisar codigo Python/JS/Go con criterios de seguridad. |
| `security-threat-model` | Crear threat models del sistema: credenciales de broker, APIs, datos, ordenes, modelos y deployment. |
| `security-ownership-map` | Mapear ownership y riesgo de codigo sensible si el repo crece con varios contributors. |
| `gh-address-comments` / `gh-fix-ci` | Corregir comentarios de PR y fallos de CI en GitHub. |

## Plugins/capacidades activas del entorno

Estas capacidades ya estan disponibles en la sesion actual aunque no fueron instaladas como skills de usuario:

- `openai-developers`: util para Agents SDK, Apps SDK, claves API y troubleshooting de OpenAI.
- `codex-security`: util para escaneos de seguridad, validacion de findings, threat modeling y fixes.
- `github`: util para PRs, issues, revisiones y publicacion.
- `build-web-apps` y `build-web-data-visualization`: utiles para dashboards, visualizaciones, paneles de monitoreo y UI.
- `superpowers`: util para planes, debugging sistematico, TDD, revision y verificacion.

## Como aplicarlas al trading bot

1. **Desarrollo LLM**
   - Usar `openai-docs` para elegir modelos, APIs, patrones de agentes, structured outputs y evaluaciones.
   - Usar `cli-creator` para comandos reproducibles: `ingest`, `train`, `backtest`, `paper`, `evaluate`, `deploy`.
   - Usar notebooks para comparar modelos locales, modelos OpenAI y baselines estadisticos.

2. **Auditoria**
   - Usar `security-threat-model` para credenciales de broker, llaves API, ordenes, datos de mercado y entorno de inferencia.
   - Usar `security-best-practices` para revisar codigo sensible.
   - Usar `sentry` para trazabilidad de fallos, errores de ejecucion y regresiones operativas.

3. **Mejora continua**
   - Usar `define-goal` para dividir el proyecto por fases: MVP, paper trading, futures, FX futures y live controlado.
   - Usar `jupyter-notebook` para experimentos y reportes reproducibles.
   - Usar `notion-research-documentation` si se decide mantener decisiones y hallazgos en Notion.

4. **Entrenamiento y evaluacion**
   - Crear pipelines versionados de datos, features, labels y modelos.
   - Mantener champion/challenger para modelos.
   - Evaluar con walk-forward, out-of-sample, costos, slippage y controles anti-leakage.
   - No permitir que un LLM envie ordenes sin pasar por risk gates deterministas.

## Nota operativa

Para que Codex cargue las skills recien instaladas como skills persistentes de usuario, hay que reiniciar Codex.
