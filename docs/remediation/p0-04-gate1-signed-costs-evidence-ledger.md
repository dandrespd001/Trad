# P0-04 — Gate 1, costes firmados y ledger de evidencia

Fecha de revisión: 2026-07-14. Estado: **implementación parcial, no apta para
promoción live**.

`live_trading_allowed` permanece en `false`. Este paquete no demuestra edge,
rentabilidad ni costes live y no autoriza capital real. Por contener
reconciliación, riesgo y promoción, fue implementado y revisado directamente
por Codex; no se delegó a MiniMax-M3.

## Qué quedó corregido

- Gate 1 exige una política esquema `2.0`, creada antes de la ventana y
  prerregistrada de forma inmutable en el ledger.
- Los límites de shortfall se evalúan por sleeve. Cada sleeve debe alcanzar su
  propio mínimo de fills y respetar su propia mediana y P90; una sleeve no puede
  ocultar a otra mediante un agregado global.
- Cada fill se obtiene de actividades broker individuales, conserva su ID
  estable y se reconcilia contra orden, símbolo, lado, cantidad acumulada,
  leaves, cantidad total y VWAP.
- El coste conserva signo: positivo es adverso y negativo es mejora. Compra y
  venta usan el mismo convenio; no se aplica `abs` a la diferencia de precio.
- Una orden no terminal, un fill ausente/huérfano/duplicado conflictivo, una
  página incompleta, evidencia no finita, política inválida o lectura broker
  fallida bloquean el reporte.
- El ledger SQLite usa WAL, sincronización completa, claves foráneas, hashes
  canónicos y cadenas append-only para políticas, fills, manifiestos e
  incidentes. Un replay idéntico es idempotente; el mismo ID con contenido
  diferente es un conflicto.
- Resolver un incidente requiere un manifiesto completo posterior. Una nota o
  reconocimiento textual no basta.
- El reporte paper puede quedar operativo en `OK`, pero siempre publica
  `economic_reconciliation_complete=false`, `promotion_eligible=false`,
  `fee_source=UNAVAILABLE_PAPER_NOT_MODELED` y seguridad paper-only.

## Política v2 prerregistrada

La política es un artefacto versionado separado. Su forma obligatoria es:

```text
schema_version: "2.0"
policy_id: identificador inmutable
created_at: timestamp UTC anterior al inicio
window: {start, end}
required_sleeves: [etf, crypto, ...]
min_complete_days: entero positivo
price_shortfall_limits_bps:
  etf: {min_reconciled_fills, max_median, max_p90}
  crypto: {min_reconciled_fills, max_median, max_p90}
max_daily_loss_pct: fracción entre 0 y 1
max_drawdown_pct: fracción entre 0 y 1
```

Los valores económicos no se incluyen aquí deliberadamente: deben aprobarse y
prerregistrarse antes de observar la ventana. No se reutiliza el umbral
histórico global de 35 bps ni se ajustan límites después de ver resultados.

Registro, antes de la ventana:

```bash
PYTHONPATH=src .venv312/bin/python -m trading_ai.cli \
  sleeve-gate1-policy-register \
  --policy RUTA_POLITICA_V2.json \
  --evidence-ledger RUTA_LEDGER.sqlite3
```

Reporte broker paper, estrictamente de lectura:

```bash
PYTHONPATH=src .venv312/bin/python -m trading_ai.cli \
  sleeve-gate1-report \
  --policy RUTA_POLITICA_V2.json \
  --evidence-ledger RUTA_LEDGER.sqlite3 \
  --real-paper --confirm-paper
```

Una política ausente, distinta de la registrada o registrada tarde produce
salida bloqueada. No se deben registrar retrospectivamente políticas para
convertir actividad histórica en evidencia promocionable.

## Alcance real del ledger actual

El ledger actual cubre:

- política canónica y ventana efectiva;
- fills individuales;
- manifiestos de cobertura de ventana;
- incidentes append-only;
- verificación read-only de integridad, completitud e incidentes abiertos.

Todavía no cubre quotes de decisión/envío/fill, fees finales, FX, correcciones o
busts, IDs de request del broker ni todos los componentes de implementation
shortfall. La completitud del manifiesto depende además de que el adaptador haya
recorrido correctamente toda la paginación broker.

Las cadenas SHA-256 detectan corrupción accidental o reescritura parcial, pero
un actor con control total de escritura puede reemplazar la base y reconstruir
los hashes. Antes de dinero real hace falta anclar periódicamente la raíz fuera
del mismo dominio de escritura mediante firma, transparencia o almacenamiento
WORM.

## Evidencia de pruebas

Las pruebas dirigidas cubren costes firmados, paginación/replay de actividades,
órdenes parciales/no terminales, reconciliación, ledger, incidentes,
prerregistro, políticas por sleeve y reejecución idempotente. La cifra de la
suite debe tomarse del run de release vigente, no copiarse como garantía
permanente.

El run de release del 2026-07-14 aprobó `1.641` pruebas; Ruff quedó limpio sobre
`78` archivos Python modificados o nuevos y `git diff --check` no encontró
errores. Esta evidencia verifica el estado del checkout auditado, no completa
los componentes económicos pendientes enumerados a continuación.

## Trabajo que falta para cerrar P0-04

1. Persistir quotes con feed, timestamps y procedencia; bloquear stale, crossed,
   futuras o feed mismatch.
2. Ingerir fees/CFEE broker, rebates y conversiones FX con signo y watermark
   final; en paper mantenerlas explícitamente no disponibles.
3. Modelar correcciones y busts idempotentes y conservar `X-Request-ID`/cursor
   de cada página.
4. Persistir gap, latency-price, spread, opportunity cost e implementation
   shortfall completos usando `Decimal`.
5. Anclar externamente las raíces del ledger y demostrar recuperación tras
   crash, replay y corrupción.
6. Ejecutar shadow live sin órdenes para validar feeds y reconciliación. Solo
   después puede diseñarse un gate separado de canary; Gate 1 paper nunca lo
   habilita por sí mismo.
