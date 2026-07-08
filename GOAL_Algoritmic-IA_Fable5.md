# GOAL — Algoritmic-IA: mejora de los modelos IA/LLM para trading algorítmico

**Proyecto:** `/home/adquiod/Documentos/Algoritmic-IA` (Python; actualmente en paper trading con conexión API directa al broker/exchange)
**Ejecutores:** Claude Fable 5 (arquitecto, revisor y gatekeeper) + MiniMax vía `claude-minimax` (implementador, en el rol que normalmente ocuparía Sonnet)
**Ventana de trabajo:** 08/07/2026 → 12/07/2026 (último día de acceso a Fable 5). Prioriza con Fable lo que más aprovecha su capacidad: arquitectura, revisión de código, diseño de validación y decisiones de riesgo.
**Mercados objetivo:** Forex, Futuros y ETFs.
**Interfaz de control:** bot de Telegram.

---

## 0. Principio rector (leer antes de empezar)

Ninguna estrategia puede garantizar rentabilidad. Por eso este goal **no** pide "asegurar ganancias": pide construir y demostrar, con evidencia out-of-sample verificable, un sistema con expectativa positiva y riesgo estrictamente acotado.

- El objetivo operativo es **maximizar el retorno ajustado por riesgo** (Sharpe/Sortino en out-of-sample) sujeto a límites duros de drawdown y pérdida diaria — no maximizar ganancias brutas.
- Está **prohibido** declarar éxito con métricas in-sample, resultados no reproducibles o afirmaciones sin artefactos (logs, CSV/Parquet, reportes generados por código realmente ejecutado en la sesión).
- Si algo no puede demostrarse, se reporta como "no verificado". Un informe honesto vale más que un número bonito.

## 1. Orquestación y protocolo de trabajo

**Roles**
- **Fable 5 (tú):** descompone este goal en tareas, escribe specs con criterios de aceptación, revisa cada diff, ejecuta los tests, valida los resultados estadísticos, toma las decisiones de arquitectura y riesgo, y aprueba o rechaza cada entrega.
- **MiniMax (vía `claude-minimax`):** implementa el código, los refactors y los tests según las specs de Fable. Ocupa el lugar de Sonnet como modelo ejecutor.

**Ciclo por tarea**
1. Fable escribe una spec corta: objetivo, archivos afectados, criterios de aceptación, tests esperados.
2. MiniMax implementa y corre los tests localmente.
3. Fable revisa el diff, ejecuta la suite completa y aprueba, o devuelve con feedback concreto.
4. Máximo 3 iteraciones por tarea. Si no converge, Fable decide: dividir la tarea, reescribir la spec, o implementar directamente el módulo (solo si es crítico para el deadline).

**Pre-flight obligatorio (primera acción de la sesión)**
- Verificar que `claude-minimax` está operativo en este entorno: que el comando/alias existe, qué modelo sirve realmente (ejecutar `/model` dentro de la sesión — en los planes gestionados de MiniMax la versión puede cambiar sin aviso), su ventana de contexto y el soporte de herramientas.
- No asumir flags ni sintaxis del wrapper: revisar `claude-minimax --help`, el script/alias local o `~/.claude/settings.json` según corresponda.
- Si no está operativo, notificarlo de inmediato y proponer plan B (Fable implementa lo crítico) en lugar de bloquearse.

## 2. Alcance de mercado

- Cada clase de activo (Forex, Futuros, ETF) requiere configuración propia y explícita: calendario y horarios de sesión, tick size y valor del punto/pip, tamaño de contrato, comisiones, spread típico, modelo de slippage, swaps overnight (forex), rollover de vencimientos (futuros) y márgenes.
- Símbolos y timeframes objetivo: `[CONFIRMAR con el humano]`.
- Broker/exchange y API: usar exclusivamente lo que ya existe en el repo y la documentación oficial de esa API. **Nunca inventar endpoints, métodos ni parámetros**; ante la duda, marcar `TODO: verificar` y preguntar.

## 3. Condiciones de ENTRENAMIENTO (modelos ML/IA y uso de LLM)

**Datos**
- Datos point-in-time, sin sesgo de supervivencia ni lookahead. Auditoría de calidad: huecos, duplicados, outliers, cambios de huso horario/DST.
- Datasets versionados (hash + rango de fechas + fuente) para garantizar reproducibilidad.

**Splits y validación**
- Separación temporal estricta train/validation/test.
- **Purged k-fold cross-validation con embargo** (metodología López de Prado, ya adoptada en el plan del proyecto) para evitar leakage entre folds.
- Checklist anti-leakage antes de cada entrenamiento: ¿alguna feature usa información futura? ¿la normalización se ajustó solo con datos de train? ¿los labels tocan datos posteriores al punto de decisión?

**Modelado**
- Baseline primero: reglas + modelos simples (p. ej. gradient boosting) que demuestren edge out-of-sample antes de complejizar.
- **RL únicamente como capa de refinamiento** sobre un edge ya demostrado (coherente con el plan de 10 fases existente). Prohibido saltar a RL sin baseline validado.

**Rol del LLM en el pipeline (definir explícitamente y por escrito)**
- El LLM **no** va en el camino crítico de ejecución tick a tick. Roles válidos: clasificación de régimen de mercado, análisis de noticias/sentimiento, sanity-check/árbitro de señales, generación de reportes.
- Presupuesto de latencia por llamada: `[CONFIRMAR]`. Si el LLM no responde a tiempo o falla, debe existir un **fallback determinista** ya codificado.
- El LLM nunca inventa precios, posiciones ni estados: solo razona sobre datos que el sistema le provee, y su salida se valida contra un esquema estricto antes de usarse.

**Reproducibilidad y anti-overfitting**
- Seeds fijas, entorno con dependencias fijadas (lockfile), registro de cada experimento (config + métricas + hash del dataset).
- Registrar cuántas configuraciones/variantes se probaron y descontar el multiple testing con **PSR / Deflated Sharpe Ratio**.
- Análisis de sensibilidad: la estrategia debe sobrevivir perturbaciones de ±20% (aprox.) en parámetros clave. Se prefiere estabilidad de parámetros a picos de optimización.

## 4. Condiciones de TESTEO

**Tests de software**
- Unit tests: indicadores, cálculo de tamaño de posición, lógica de TP/SL dinámico, parsers de la API.
- Integración: ciclo completo de orden (crear → modificar TP/SL → cerrar) contra un broker simulado/mocks, incluyendo fills parciales y rechazos.
- End-to-end en paper trading.

**Backtesting realista**
- Costes completos: comisión + spread variable + slippage + swaps/rollover.
- Fills conservadores (asumir el peor precio razonable) y latencia simulada.
- Prohibido reportar backtests "sin fricciones" como resultado del sistema.

**Validación estadística (umbrales propuestos — aproximados y ajustables con el humano)**
- Walk-forward out-of-sample con, orientativamente: Sharpe ≥ ~1.0, Profit Factor ≥ ~1.3, Max Drawdown ≤ ~15%, mínimo ~100 trades OOS, Deflated Sharpe Ratio > 0.
- Monte Carlo sobre la secuencia de trades: drawdown p95 dentro del límite tolerado.
- Estos números son criterios de trabajo, no garantías; confirmarlos con el humano antes de usarlos como gate.

**Stress tests**
- Ventanas históricas extremas (p. ej. marzo 2020, 2022), gaps de apertura semanal en forex, vencimientos/rollover en futuros, baja liquidez.
- Fallos operativos: desconexión de la API a mitad de posición, reinicio del proceso, respuestas malformadas del broker, reloj desincronizado.

**Regla de oro:** ningún resultado cuenta sin su artefacto (ruta al log/CSV/reporte generado en esta sesión) referenciado en el informe.

## 5. Condiciones de EJECUCIÓN LIVE (gates escalonados)

- **Gate 0 — Paper (estado por defecto).** `live` desactivado por flag de configuración. Nada pasa a live automáticamente, jamás.
- **Gate 1 — Paper validado.** `[N]` días o `[M]` trades en paper con: tracking error aceptable frente al backtest, cero errores críticos de ejecución/reconciliación y métricas dentro de la banda esperada.
- **Gate 2 — Live canario (solo con confirmación humana explícita en el momento).** Capital y tamaño de posición mínimos. Límites duros aproximados (confirmar): riesgo por trade ≤ 0.5–1% del equity; pérdida diaria máx. 2–3% → pausa automática del día; kill-switch por drawdown de 8–10% → cerrar todo y detener el sistema; tope de posiciones concurrentes y de exposición por clase de activo.
- **Gestión dinámica de posiciones (núcleo del proyecto).** Reglas codificadas y testeadas para modificar TP/SL de posiciones abiertas: trailing por ATR/volatilidad, mover SL a break-even tras +1R (configurable), tomas parciales. Regla dura por defecto: **nunca ampliar el SL** más allá del riesgo inicial de la operación.
- **Robustez operativa.** Reconciliación de posiciones/órdenes contra el broker al arrancar y de forma periódica; reintentos idempotentes; recuperación de estado tras reinicio; manejo correcto de husos horarios; watchdog + heartbeat.
- **Nota de realismo.** Dentro de la ventana al 12/07 se entrega la *capacidad* live con todos los gates implementados y probados en paper. La promoción a dinero real depende de cumplir Gate 1 (puede requerir más días de paper) y es decisión exclusivamente humana. La revisión de la regulación del broker y de los efectos fiscales (Colombia) también queda del lado humano.

## 6. Control por Telegram

**Comandos mínimos:** `/status`, `/positions`, `/pnl [dia|semana|total]`, `/pause`, `/resume`, `/close <id>`, `/close_all`, `/risk get|set <param> <valor>`, `/mode paper|live`, `/report`, `/kill`.

**Seguridad:** whitelist de `chat_id` autorizados; token del bot solo en variables de entorno/secretos (nunca en el repo ni en logs); confirmación en dos pasos para acciones destructivas (`/close_all`, `/mode live`, `/kill`, cambios de riesgo); rate limiting; log de auditoría de todos los comandos recibidos.

**Alertas push:** apertura/cierre de posiciones, modificaciones de TP/SL, límites de riesgo alcanzados, errores de API, activación del kill-switch, resumen diario de PnL y estado.

## 7. Entregables (Definition of Done)

1. Informe de auditoría inicial: estado actual del repo frente a este goal, con backlog priorizado.
2. Código implementado con la suite de tests pasando (módulos críticos con buena cobertura).
3. Reporte de validación: CV purgada + walk-forward + DSR + Monte Carlo, con rutas a todos los artefactos.
4. Ejecución paper end-to-end supervisada sin errores críticos.
5. Runbook operativo: arranque, parada, recuperación tras fallo, rollback y operación vía Telegram.
6. Checklist de gates con su estado real (cumplido / pendiente) y próximos pasos posteriores al 12/07.
7. Informe final honesto: métricas reales, limitaciones conocidas, riesgos abiertos. Prohibido el lenguaje de "rentabilidad garantizada" en cualquier documento, código, interfaz o mensaje de Telegram.

## 8. Reglas de honestidad y seguridad para ambos agentes

- No inventar funciones, endpoints, librerías ni parámetros: verificar contra el código del repo o la documentación oficial. Ante la duda: `TODO: verificar` + pregunta al humano.
- Toda métrica reportada proviene de código ejecutado en la sesión, con artefacto adjunto.
- Incertidumbre explícita siempre; nada de suposiciones silenciosas para "rellenar huecos".
- Credenciales y API keys jamás en el código, en commits ni en logs.
- Ninguna orden real ni cambio paper→live sin confirmación humana explícita en ese instante.
- El código de MiniMax puede ser correcto pero menos cuidadoso en casos borde: la revisión de Fable no es opcional en ningún diff.

## 9. Plan de trabajo sugerido (08–12 de julio de 2026)

- **Mié 08/07:** pre-flight de `claude-minimax`; auditoría del repo (apoyarse en el prompt de auditoría de 7 dimensiones ya existente); backlog priorizado y specs (Fable).
- **Jue 09/07:** implementación del núcleo vía MiniMax: gestión dinámica de TP/SL, motor de límites de riesgo, esqueleto del bot de Telegram. Revisión de Fable + tests.
- **Vie 10/07:** pipeline de entrenamiento/validación (CV purgada, walk-forward, DSR), backtesting realista por clase de activo, stress tests.
- **Sáb 11/07:** integración end-to-end en paper, hardening operativo (reconciliación, recuperación, watchdog), Telegram completo con su capa de seguridad.
- **Dom 12/07:** ejecución paper supervisada, correcciones finales, documentación, informe de cierre y handover (el runbook debe permitir continuar después del 12/07 con MiniMax u otro modelo como ejecutor).

Si el tiempo aprieta: recortar alcance (menos símbolos, o una sola clase de activo primero) antes que recortar tests, validación o gates.

## 10. Parámetros a confirmar con el humano ANTES de empezar

- Broker/exchange y API exacta ya integrada en el repo.
- Símbolos y timeframes por clase de activo.
- Capital de referencia y umbrales de riesgo definitivos (por trade, diario, drawdown).
- Rol exacto del LLM en el pipeline y su presupuesto de latencia.
- Modelo real detrás de `claude-minimax` (verificar con `/model`) y sus límites de contexto/herramientas.
- Duración mínima aceptable del Gate 1 (paper) antes de considerar live.
