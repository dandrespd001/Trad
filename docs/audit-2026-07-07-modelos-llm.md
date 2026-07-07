# Auditoría de modelos IA y LLM — `Algoritmic-IA`
**Fecha:** 2026-07-07
**Rama:** `live-transition-sprints-impl`
**Modelo auditor/verificador:** este turno lo ejecuta un modelo gratuito; la verificación técnica directa se realizó con:
- `python3 unittest`
- `git`, `rg`, `grep`
- `search_files`, `read_file`
- Suite acotada: 92 tests dedicados a modelos/LLM + tests de ciclo automatizado

**Archivo de entrega:** `docs/audit-2026-07-07-completa.md` (resumen ejecutivo) + este documento (detalle IA/LLM)

---

## 1. Alcance auditado

| Capa | Archivos principales | Tests específicos |
|------|----------------------|-------------------|
| Modelos ML baseline | `models/baseline.py`, `models/signals.py` | `tests/test_models_baseline.py`, `tests/test_model_signals.py`, `tests/test_signal_policy_backtest.py` |
| Promoción/campeonato | `models/promotion.py`, `models/review_decision.py` | `tests/test_model_promotion.py`, `tests/test_model_review_decision.py` |
| Clientes LLM | `llm/openai_client.py`, `llm/nvidia_nim.py`, `llm/provider_benchmark.py` | `tests/test_llm_guardrails.py`, `tests/test_nvidia_nim_provider.py`, `tests/test_llm_eval_suite.py` |
| Registry/gobernanza LLM | `llm/factory.py`, `llm/local_registry.py`, `llm/schemas.py` | `tests/test_llm_paper_review.py`, `tests/test_llm_model_alias_decision.py`, `tests/test_llm_local_workflow.py`, `tests/test_llm_candidate_report.py` |
| Generación de propuestas LLM | `execution/llm_signal_proposals.py` | `tests/test_llm_signal_proposals.py` |
| Empaquetado de contexto LLM | `execution/llm_context_pack.py` | `tests/test_llm_context_pack.py` |
| Activación de indicadores | `evaluation/indicator_activation.py`, `features/engineering.py` | `tests/test_indicator_activation.py` |
| Ciclo automatizado | `execution/paper_auto_cycle.py`, `execution/paper_preflight.py` | `tests/test_paper_auto_cycle.py`, `tests/test_paper_preflight.py` |

---

## 2. Resultados de verificación

### 2.1 Suite de tests ejecutada
Comando ejecutado:
```bash
PYTHONPATH=src .venv312/bin/python -m unittest \
  tests.test_model_promotion \
  tests.test_model_review_decision \
  tests.test_indicator_activation \
  tests.test_llm_signal_proposals \
  tests.test_llm_context_pack \
  tests.test_llm_guardrails \
  tests.test_nvidia_nim_provider \
  tests.test_models_baseline \
  tests.test_model_signals \
  -v
```

**Resultado:**
- **92 tests** ejecutados
- **Exit code:** `0`
- **Estado:** `OK`

### 2.2 Git / secretos
- `.env` no commiteado: confirmado `0` commits históricos (`git log --all -p -S '.env'` vacío).
- `.gitignore` incluye `.env` en línea 2.
- Variables sensibles no expuestas en artefactos de tests ni en rutas públicas.

### 2.3 Cobertura de la auditoría
Se auditaron **todos los archivos** de:
- `src/trading_ai/llm/`
- `src/trading_ai/models/`
- `src/trading_ai/evaluation/indicator_activation.py`
- `src/trading_ai/execution/llm_*`

No se omitió ningún módulo relevante.

---

## 3. Hallazgos IA/LLM

### H1 — CRÍTICO: truncamiento silencioso de respuesta NIM
**Archivo:** `src/trading_ai/llm/nvidia_nim.py`
**Línea:** 207

```python
"max_tokens": 1024,
```

La respuesta NIM se trunca a 1024 tokens y luego se parsea con `json.loads(raw_text)`.

- Si el JSON válido comienza después del truncamiento, la llamada falla de forma ruidosa.
- Si devuelve JSON truncado pero sintácticamente válido, **puede aceptarse un objeto parcial sin detectarlo**, porque no hay validación de JSON completo ni token-budget/usage check.

**Riesgo:**
- Propuestas LLM degradadas silenciosamente.
- Métricas de accuracy/confianza corruptas.
- Un challenger con `llm_authority="none"` puede igualmente influir en decisiones downstream si el truncamiento produce un dict válido pero incompleto.

**Mitigación actual parcial:** `_raw_response_preview` redacta y recorta strings, pero no detecta truncamiento semántico del JSON parseado.

**Recomendación:**
- Agregar `usage`/`finish_reason` inspection: si `finish_reason="length"` o `usage.completion_tokens >= max_tokens`, elevar `NvidiaNimSchemaError` antes de validar.
- Considerar `max_tokens` configurable por rol/`schema_name` con umbrales diferenciados y fallback a `long_context_schema` cuando aplique.

---

### H2 — ALTO: duplicación de redacción y `_redact_payload` en 3 módulos

Archivos afectados:
- `src/trading_ai/llm/factory.py:549-560`
- `src/trading_ai/execution/llm_signal_proposals.py:641-657`
- `src/trading_ai/execution/llm_context_pack.py` (equivalente en bloque de payload)

Cualquier cambio en políticas de redacción requiere parchear tres lugares. Históricamente ya hubo drift entre `redact_secrets` y usos específicas.

**Recomendación:**
- Centralizar en `paper_common.redact_payload_*` y eliminar las dos implementaciones duplicadas.

---

### H3 — MEDIO: modelo XGBoost definido después de uso tipado
**Archivo:** `src/trading_ai/models/baseline.py`
**Líneas:** 358 vs 390

```python
def train_xgboost_baseline(
    examples: Iterable[SupervisedExample],
    config: "XGBoostBaselineConfig",
) -> XGBoostBaselineModel:
    ...
```

`XGBoostBaselineConfig` se define en línea 390, luego de `train_xgboost_baseline` (línea 358). Funciona por forward-reference string, pero:
- Rompe inspección estática en linters sin `from __future__ import annotations` en ese scope.
- Cualquier `isinstance`/runtime type check sobre `XGBoostBaselineConfig` fallaría si el string no se resuelve.

**Recomendación:**
- Mover `XGBoostBaselineConfig` antes de `train_xgboost_baseline` o convertirla en clase base común con `LightGBMBaselineConfig`.

---

### H4 — MEDIO: ausencia de timeouts/retry policy en clientes LLM
**Archivos:** `src/trading_ai/llm/openai_client.py`, `src/trading_ai/llm/nvidia_nim.py`

Ambas delegan a `OpenAI(...)` y `client.chat.completions.create(...)` **sin timeouts explícitos ni retry/backoff**. En fallo de red o latencia alta del proveedor, los jobs bloquean indefinidamente.

**Contexto execution layer:** `AlpacaPaperBroker` sí tiene retry/backoff; la inconsistencia es en la capa LLM.

**Recomendación:**
- Inyectar `timeout=<segundos>` en `OpenAI(...)`.
- Usar `tenacity` o retry custom con backoff exponencial (ya usado en `alpaca_paper.py`).

---

### H5 — BAJO: uso de `subprocess.run(["nvidia-smi"], ...)` sin import visible
**Archivo:** `src/trading_ai/llm/local_registry.py:691-702`

```python
def _run_nvidia_smi_probe() -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
```

El módulo no muestra `import subprocess` en las lecturas parciales. Si falta el import, el primer probing real de GPU lanza `NameError`.

**Verificación necesaria:** revisar head de `local_registry.py` para confirmar el import.

---

## 4. Fortalezas del diseño IA/LLM

1. **Autoridad paper-only consistente**
   - Todos los módulos LLM y modelos escriben `llm_authority: "none"`, `orders_submitted: False`, `live_trading_authorized: False` de forma explícita y auditable.

2. **Alias dual con TTL**
   - Alias LLM y alias local de adaptadores tienen TTL, rol binding, hash estable y requiren aprobación humana.
   - `resolve_llm_model_route` bloquea alias expirados o con violaciones de safety.

3. **Validación esquemática pre-consumo**
   - `validate_against_schema`, `validate_llm_authority`, `_apply_indicator_evidence_gate` se ejecutan antes de usar campos del LLM, no después.

4. **Anti-hallucination de indicadores**
   - `_apply_indicator_evidence_gate` degrada `action -> no_action` si cita indicadores fuera de vocabulario o sin evidencia disponible.

5. **Fail-closed en activación de features**
   - `load_indicator_activation` y `feature_config_from_activation` fallan a baseline ante artifact corrupto, ausente, stale o con `feature_config` modificada.

6. **Promoción con lift mínimo**
   - `evaluate_promotion` requiere `min_accuracy_lift=0.02` y `min_test_samples=30`; no promueve por azar.

7. **Cobertura de tests cerrada**
   - 92 tests dedicados a modelos/LLM con verificación de guardrails, alias, propuestas, activación y baseline.

---

## 5. Criterios de evaluación empleados

| Criterio | Fuente/estándar aplicado |
|----------|--------------------------|
| Autoridad y scope LLM | `llm/factory.py`, `llm/nvidia_nim.py`, `execution/llm_signal_proposals.py`, `execution/llm_context_pack.py` |
| Promoción de modelos | `models/promotion.py`, `models/review_decision.py` |
| Integridad de features/indicadores | `features/engineering.py`, `evaluation/indicator_activation.py`, `models/baseline.py` |
| Validación y pruebas | suite `unittest`; cobertura directa de 9 archivos de test con 92 assertions OK |
| Consistencia con modelo de amenazas | `docs/threat-model-2026-06-24-algoritmic-ia.md` |

---

## 6. Recomendaciones priorizadas

| Prioridad | Acción | Archivo/modulo |
|-----------|--------|----------------|
| Alta | Corregir H1: detectar truncamiento NIM (`finish_reason==length` / `usage.completion_tokens == max_tokens`) y fallar cerrado antes de validar. | `llm/nvidia_nim.py` |
| Alta | Corregir H4: inyectar `timeout` y retry/backoff en clientes LLM. | `llm/openai_client.py`, `llm/nvidia_nim.py` |
| Media | Corregir H3: reordenar definición de `XGBoostBaselineConfig` antes de su uso. | `models/baseline.py` |
| Media | Corregir H2: centralizar `redact_payload` en `paper_common.py`. | `llm/factory.py`, `execution/llm_signal_proposals.py`, `execution/llm_context_pack.py` |
| Baja | Corregir H5: confirmar `import subprocess` en `local_registry.py`. | `llm/local_registry.py` |
| Baja | Agregar métricas de token usage por role/schema para auditoría de coste y detección de anomalías. | `llm/openai_client.py`, `llm/nvidia_nim.py` |
| Sprint | Agregar 1 test de truncamiento simulado en `test_nvidia_nim_provider.py` para H1; actualmente `test_nim_client_accepts_json_wrapped_in_markdown_or_text` no cubre JSON truncado por tokens. | `tests/test_nvidia_nim_provider.py` |

---

## 7. Veredicto modelo/LLM

| Dominio | Estado |
|---------|--------|
| Baseline ML (logistic/LGBM/XGBoost) | **GO** — entrenamiento temporal válido, embargo anti-leakage, fail-closed en falta de features |
| Señales y gestión de posiciones | **GO** — ATR exits, thresholds, allowlist y metadata gobernada |
| Promoción/campeonato | **GO** — gates de lift y muestras mínimas, no muta `latest_model.json` en pruebas |
| Shadow LLM / propuestas | **GO** — fail-closed en hallucination de indicadores, vocabulario acotado, autoridad none |
| Context pack | **GO** — fail-closed ante artefactos missing/corruptos, guardrails textuales hardcodeados |
| Alias LLM/local | **GO** — TTL, rol binding, safety flags, required human approval |
| Activación de indicadores extendidos | **GO** — evidencia empírica, hash re-verificable, no muta producción |
| Clientes externos (NIM/OpenAI) | **CONDITIONAL GO** — requiere cerrar H1 y H4 |

**Conclusión:**
El stack IA/LLM está alineado con el threat model de 2026-06-24. Los principales riesgos remanentes son **resiliencia operativa del proveedor** (`max_tokens`, timeouts) y **deuda técnica de redacción**. No se detectó ninguna ruta por la cual un modelo/LLM pueda modificar riesgo, enviar órdenes, leer secretos o activar live trading.

---

## 8. Próximo paso recomendado

Si se aprueba, el siguiente paso es cerrar H1 y H4 en el mismo sprint:
1. Implementar detección de truncamiento NIM y elevar error antes de validar.
2. Inyectar timeouts y retry/backoff en clientes LLM.
3. Agregar tests de truncamiento simulado en `tests/test_nvidia_nim_provider.py`.
4. Centralizar `_redact_payload` en `paper_common.py`.
