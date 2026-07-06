# Auditoria offline completa - 2026-06-25

## Alcance

- Repositorio: `/home/adquiod/Documentos/Algoritmic-IA`.
- Interprete usado para gates: `.venv312/bin/python` (`Python 3.12.13`).
- Modo de auditoria: offline, sin broker, sin red intencional y sin lectura de credenciales.
- Fecha de ejecucion: 2026-06-25.

## Cambios realizados

- Se movio el reporte historico `Algoritmic-IA-threat-model.md` a
  `docs/threat-model-2026-06-24-algoritmic-ia.md`.
- Se agrego una nota inicial al threat model movido para indicar que es un reporte
  historico/reconciliado y que varios hallazgos ya no reflejan el codigo actual.
- Se redujo la deuda de typing en tests: payloads JSON flexibles usan `Any`, algunos
  helpers aceptan `Mapping`, y fakes/casts locales estrechan objetos dinamicos donde
  mypy no puede inferir la forma real del fixture.
- No se cambiaron APIs ni comportamiento de produccion.

## Comandos ejecutados

| Comando | Resultado |
| --- | --- |
| `.venv312/bin/python --version` | PASS: `Python 3.12.13` |
| `.venv312/bin/python -m ruff check src tests --no-cache` | PASS antes de los cambios de typing; PASS despues de ordenar imports |
| `.venv312/bin/python -m mypy src/trading_ai --cache-dir /tmp/mypy-cache-src-audit` | PASS: 96 source files |
| `.venv312/bin/python -m mypy src/trading_ai tests --cache-dir /tmp/mypy-cache-full-audit` | FAIL inicial: 652 errores en 59 tests; PASS despues: 176 source files |
| `.venv312/bin/python -m pip check` | PASS: no broken requirements; pip desactivo cache por permisos |
| `.venv312/bin/python -m json.tool notebooks/benchmark_baseline_vs_ml_vs_timeseries.ipynb` | PASS: JSON valido |
| `./scripts/verify-paper-artifacts.sh` | PASS tras mover el threat model fuera de la raiz |

## Hallazgos confirmados

1. `Algoritmic-IA-threat-model.md` en la raiz era un artifact no trackeado que
   bloqueaba `verify-paper-artifacts.sh`. La politica del script rechaza `.md`
   no trackeados en la raiz como artifacts generados fuera de `reports/tmp`.
   Estado: corregido moviendo el reporte a `docs/`.

2. `mypy src/trading_ai tests` no pasaba por deuda de tipos en tests, principalmente
   payloads JSON anotados como `dict[str, object]`, acceso a estructuras anidadas
   sin casts y fakes con firmas mas estrechas que las clases base.
   Estado: corregido; `mypy src/trading_ai tests` pasa.

3. La revision offline no encontro un bug nuevo de produccion que exigiera cambiar
   APIs publicas o comportamiento runtime. Los cambios quedaron acotados a docs y
   typing de tests.

## Hallazgos historicos descartados o mitigados por evidencia actual

1. Threat model historico: "risk state fail-open / kill-switch no latching".
   Evidencia actual: `load_risk_state` falla cerrado ante archivo ausente,
   JSON corrupto o checksum alterado; `save_risk_state` escribe atomico con
   checksum; `evaluate_kill_switch` conserva el latch hasta reset explicito.
   Ver `src/trading_ai/execution/paper_risk_state.py`.

2. Threat model historico: "sin gate de horas de mercado ni sanity de precio".
   Evidencia actual: `AlpacaPaperBroker.submit_order` rechaza compras en dias no
   operables via `is_trading_day`; para envios reales paper (`dry_run=False`)
   exige `reference_price`, proveedor de market data y banda de desviacion
   `max_price_deviation_pct`. Ver `src/trading_ai/execution/alpaca_paper.py`.

3. Contencion paper/live.
   Evidencia actual: el cliente Alpaca se construye con `paper=True`, los defaults
   mantienen `live_trading_allowed: false`, y `verify-safety-patterns.py --mode live`
   busca asignaciones o mappings con `true` en `src`, `configs`, `scripts`, `docs`,
   `README.md` y `.github`.

4. Funcion objetivo riesgo-retorno.
   Evidencia actual: el sistema sigue en modo canary paper con `paper_notional_usd`
   y `sizing_mode: fixed_notional` por defecto. Esto es una limitacion cuantitativa
   conocida para evolucion futura, no un bloqueo de release offline paper-only.

## Riesgos residuales

- El estado de riesgo sigue teniendo path local por defecto
  `reports/tmp/paper_risk_state.json`. En contenedor/cloud se debe montar volumen
  persistente o configurar una ruta durable. La mitigacion actual falla cerrado si
  falta el archivo, lo que evita sobre-operar pero puede bloquear operacion paper.
- El sanity de precio en ordenes paper reales depende de inyectar un proveedor de
  market data. Si falta o no devuelve precio valido, la compra se bloquea.
- Este reporte no autoriza live trading. La conclusion solo cubre auditoria offline
  y operacion paper-only.

## Decision

- Paper-only offline: apto para continuar; la verificacion final quedo completa en
  verde.
- Live trading: no autorizado por esta auditoria; permanece fuera de alcance.

## Verificacion final

| Comando | Resultado final |
| --- | --- |
| `.venv312/bin/python -m ruff check src tests --no-cache` | PASS |
| `.venv312/bin/python -m mypy src/trading_ai --cache-dir /tmp/mypy-cache-src-audit` | PASS: 96 source files |
| `.venv312/bin/python -m mypy src/trading_ai tests --cache-dir /tmp/mypy-cache-full-audit` | PASS: 176 source files |
| `.venv312/bin/python -m pip check` | PASS: no broken requirements; pip desactivo cache por permisos |
| `.venv312/bin/python -m json.tool notebooks/benchmark_baseline_vs_ml_vs_timeseries.ipynb` | PASS |
| `./scripts/verify-paper-artifacts.sh` | PASS |
| `./scripts/verify-release.sh` | PASS: suite completa, safety scans, ruff critico, mypy acotado, pip-audit dry-run y bandit |
| `git diff --check` | PASS dentro de `verify-release.sh` |

Notas no bloqueantes: la suite emitio un `DeprecationWarning` de `websockets.legacy`
desde dependencia externa y `pip` aviso que desactivo cache por permisos del home.
