# Contrato de datos IEX para la siguiente campaña de señal

Fecha de decisión: 2026-07-27.

## Decisión

Estado: **HOLD — falta regenerar la fuente homogénea**.

La próxima campaña de señal no puede ejecutarse sobre una concatenación
Yahoo→IEX ni sobre un CSV IEX cuyo ajuste de corporate actions sea implícito.
Yahoo permanece permitido exclusivamente para desarrollo retrospectivo no
promovible.

La cadena aceptable es:

```text
Alpaca Market Data / IEX
  -> StockBarsRequest 1Day, Adjustment.ALL, UTC
  -> CSV canónico + sidecar 1.1 con contrato `iex-1.0`
  -> importación local con sidecar y hashes verificados
  -> Parquet, manifest y catálogo con procedencia upstream preservada
```

## Contrato congelado

- proveedor upstream: `alpaca_market_data`;
- feed: `iex`;
- frecuencia: `1d`;
- ajuste: `all`;
- corporate actions: literal verificable `alpaca_adjustment_all`;
- timezone de solicitud y barra: `UTC`;
- timestamp diario: `XNYS_exchange_session_date`;
- calendario: `XNYS`;
- contrato de calendario: `xnys-full-day-2024-2028-v2`;
- hash de calendario:
  `886eed762ac68ac5e90520d79a3b67b0c09a93c8aa2f4a69bedf434a7bf7ceb0`;
- hash de implementación ejecutable del calendario:
  `a7a8ecef40f9285849895a7780d2b8033af4993eac308efbb039dff06ee18dea`;
- watermark: última sesión XNYS cerrada a las 16:00
  `America/New_York`; las sesiones de cierre temprano se admiten de forma
  conservadora sólo desde las 16:00;
- tiempo de generación: `generated_at` timezone-aware; importador y consumidor
  vuelven a calcular el watermark desde ese instante y exigen coincidencia
  exacta;
- universo: exactamente `configs/universe.yml`;
- SDK: paquete y versión exactos en sidecar;
- hashes obligatorios:
  - CSV;
  - sidecar;
  - configuración de universo;
  - código normalizador;
  - implementación ejecutable del calendario;
  - dataset Parquet;
  - manifest y catálogo.

La importación verifica que cada símbolo tenga exactamente el conjunto de
sesiones XNYS esperado. Un sidecar sin el contrato `iex-1.0`, alterado, con
defaults implícitos, `status != OK`, `published != true`, blockers, hash
incorrecto, cliente inyectado, cobertura incompleta, calendario distinto o
reloj inyectado termina en `BLOCKED`. Un fin de rango en fin de semana o
feriado se valida contra la última sesión gobernada del rango, no contra el
literal `end`; esa sesión no puede ser posterior al watermark recalculado.

## Implementación

Archivos:

- `src/trading_ai/data/alpaca_market_data.py`
  - fija `DataFeed.IEX` y `Adjustment.ALL`;
  - usa límites de solicitud UTC explícitos;
  - produce sidecar 1.1 compatible con operación y contrato IEX `iex-1.0`;
  - rechaza rangos fuera del snapshot o posteriores a la sesión cerrada antes
    de construir o invocar el cliente;
  - marca clientes o relojes inyectados como no elegibles para importación
    gobernada.
- `src/trading_ai/data/market_calendar.py`
  - congela las sesiones 2024–2028 con fuentes NYSE, versión y SHA-256;
  - incluye el cierre extraordinario del 9 de enero de 2025;
  - publica un hash separado de la lógica ejecutable que interpreta el snapshot;
  - calcula un watermark conservador y timezone-aware.
- `src/trading_ai/data/catalog.py`
  - permite `provider=alpaca_market_data` sólo con atestación válida;
  - conserva la procedencia upstream dentro de manifest y catálogo;
  - conserva `generated_at` y recalcula de forma independiente el watermark;
  - incorpora copias inmutables `source.csv` y `source_attestation.json` al
    paquete aprobado y verifica sus digests después de copiarlas;
  - valida cobertura completa por símbolo y sesión;
  - mantiene la ruta `manual_csv` compatible y sin red.
- `src/trading_ai/evaluation/approved_package.py`
  - centraliza el gate read-only de paquete para todos los consumidores;
  - reabre CSV y sidecar locales, revalida SDK, normalizador, configuración,
    calendario, causalidad y cobertura XNYS exacta;
  - bloquea antes de crear directorios de resultados.
- `src/trading_ai/evaluation/approved_data.py`
  - exige y propaga versión, hash, `generated_at` y watermark;
  - recalcula el watermark antes de consumir la atestación;
  - vuelve a calcular el hash local y falla cerrado ante drift.
- `src/trading_ai/evaluation/model_research.py` y
  `src/trading_ai/evaluation/trading_model_benchmark.py`
  - consumen el mismo gate común y propagan la atestación;
  - exigen `from <= to <= as_of` antes de escribir artefactos.
- `src/trading_ai/cli.py`
  - incorpora `import-approved-data --source-attestation`.

Hashes de la implementación revisada:

```text
market_calendar.py       a7a8ecef40f9285849895a7780d2b8033af4993eac308efbb039dff06ee18dea
alpaca_market_data.py    7f89623887e46e8ad48547d94dd06d82ed0f1bb6bd59282cf4cf3edf6eac98db
catalog.py               0c8d620e75ba357b47af539657a4ed79d836987d5a9e74e46b6644afada8f709
approved_package.py      a93b6e7e6a5c2a60c583d4725e76e2d2259467968b4b369a8947ce5b309d34aa
approved_data.py         336e0284d76a1fe9f37d26b067eadbed1a953d1f35c6c8de3f683f52eca9c73e
model_research.py        25482228642a1668a367d5aea146a550103b53d4b18c29b3228c731dc3618d4d
trading_model_benchmark  28d47a949ba64025c869868812b02e9d824bab085100edc47930fb0d62182574
universe.yml           95090d4b670cde6d38442811ba06f1b4c78ba4e3f69b98bd03ef52959654f2a4
data_sources.yml       9a00fe5309dc6cfd8b85e5718a35424194e526b5ef3a6bc27f0d324ce6183fa5
```

Los flujos genéricos conservan `manual_csv` para investigación retrospectiva.
Un resultado genérico `CANDIDATE_READY` o `eligible_for_paper_challenger` no
autoriza esta campaña: el futuro runner preregistrado debe invocar el loader
con `required_provider=alpaca_market_data` y verificar todos los hashes
congelados de este contrato.

## Fuentes actuales

`data/incoming/history_5y.csv` no es promovible bajo este contrato. Su sidecar
es 1.0 y no contiene `source_sha256`, política de ajuste, semántica temporal,
estado `published`, versión de SDK ni hashes del universo/normalizador.
Retrocompletar esos campos después del hecho sería falsificar procedencia.

`data/incoming/fresh_source.csv` tiene sidecar 1.1, pero sólo cubre
2026-03-26..2026-07-24 y también precede al contrato IEX `iex-1.0`.

## Comandos posteriores al desbloqueo

Primero, obtener un archivo nuevo sin sustituir los snapshots existentes:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv312/bin/python \
  -m trading_ai.cli fetch-market-data \
  --config configs/universe.yml \
  --from 2024-01-02 \
  --to 2026-07-28 \
  --output data/incoming/core_etfs_iex_1d_2024-01-02_2026-07-28.csv
```

Después, importar como dataset separado:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv312/bin/python \
  -m trading_ai.cli import-approved-data \
  --source data/incoming/core_etfs_iex_1d_2024-01-02_2026-07-28.csv \
  --source-attestation \
    data/incoming/core_etfs_iex_1d_2024-01-02_2026-07-28.csv.fetch.json \
  --dataset-id core_etfs_iex_signal_v1 \
  --frequency 1d \
  --config configs/universe.yml \
  --provider alpaca_market_data \
  --license-note "Alpaca IEX read-only research source" \
  --as-of-date 2026-07-28
```

La descarga intentada durante esta revisión se detuvo antes de construir un
cliente o contactar al proveedor porque el proceso no dispone de
`ALPACA_PAPER_API_KEY` ni `ALPACA_PAPER_SECRET_KEY`. No se creó fuente parcial
y no se modificó el snapshot aprobado actual.

## Bloqueos arquitectónicos restantes

El gate general permanece en `HOLD` aunque productor, importador y consumidor
ya compartan el mismo contrato:

- `data/alpaca_market_data.py` conserva dependencias históricas hacia módulos de
  `execution`; deben extraerse a una frontera read-only neutral;
- todavía no existe una generación IEX real producida con el contrato
  `iex-1.0`;
- falta integrar el núcleo puro del benchmark de exposición con el runner
  genérico `next_open_v2`.

El importador ya rechaza clientes inyectados, versiones de SDK distintas,
sesiones interiores ausentes, symlinks de fuente y cambios concurrentes
detectables; además publica las generaciones IEX nuevas mediante staging y
rename de directorio, sin sobrescribir un dataset existente.

## Gate de salida

El estado cambia a `GO_FOR_PREREGISTRATION` únicamente cuando:

1. el fetch nuevo termina `OK` y `published=true`;
2. el sidecar 1.1/`iex-1.0` y el CSV verifican sus hashes;
3. la importación genera un dataset separado sin sobrescritura;
4. cada símbolo cubre exactamente las sesiones XNYS de 2024-01-02 a la fecha
   cerrada;
5. manifest y catálogo conservan el bloque `source_attestation`;
6. el consumidor de investigación revalida y propaga ese bloque;
7. calendario y watermark de sesión cerrada quedan versionados;
8. las pruebas aisladas de datos y el gate estático permanecen verdes.

Los puntos 6 y 7 ya están implementados y probados. El punto 8 está verde para
el scope focal; no sustituye la suite de liberación completa.

Ninguno de estos gates habilita paper, live o promoción.
