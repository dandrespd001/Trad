# P0-05 — contención y preparación de cutover, 2026-07-27

## Estado

**Contención aplicada / cutover bloqueado.**

No se instalaron servicios, no se contactó al broker y no se modificaron
posiciones ni credenciales.

## Unidades identificadas

| Unidad | Propósito | Estado tras contención |
|---|---|---|
| `trading-n0.timer` → `trading-n0.service` | campaña N0 | `disabled`, `inactive` |
| `trading-crypto-sleeve.timer` → `trading-crypto-sleeve.service` | sleeve cripto | `disabled`, `inactive` |
| `trading-position-watch.timer` → `trading-position-watch.service` | observación de posiciones | `disabled`, `inactive` |
| `trading-ai-paper-executor.service` | futuro executor IPC único | no instalado |

La contención se aplicó con:

```bash
systemctl --user disable --now \
  trading-n0.timer \
  trading-crypto-sleeve.timer \
  trading-position-watch.timer
```

Se verificó que no quedaran timers programados.

## Reactivación observacional futura

Antes de habilitar cualquier unidad:

1. obtener un snapshot paper broker estrictamente read-only fuera de logs;
2. reconciliar posiciones, órdenes, fills, journal y estado de riesgo;
3. verificar checksum y fecha del estado de riesgo;
4. ejecutar observabilidad, monitor, campaign, performance, ops check e índice
   de evidencia para una misma fecha ISO;
5. mantener `BLOCKED` ante `CRITICAL`, `ERROR`, discrepancia, estado stale o
   kill switch activo.

Sólo después de una reconciliación sin divergencias puede considerarse:

```bash
systemctl --user enable --now trading-position-watch.timer
```

`trading-n0.timer` y `trading-crypto-sleeve.timer` permanecen deshabilitados
hasta cerrar P0-05. La reactivación del watch no autoriza mutaciones.

## Validación del paquete systemd

`deploy/systemd/trading-ai-paper-executor.service` pasó:

```bash
systemd-analyze verify deploy/systemd/trading-ai-paper-executor.service
systemd-analyze security --offline=yes \
  deploy/systemd/trading-ai-paper-executor.service
```

Resultado de hardening: exposición `1.5 OK`.

Aspectos deliberadamente abiertos:

- AF_UNIX es necesario para IPC.
- AF_INET/AF_INET6 son necesarios para el broker paper.
- `PrivateNetwork=yes` no es compatible con ese acceso.
- `IPAddressDeny=any` bloquearía el broker y requiere una allowlist estable,
  todavía no definida.
- endurecer más el filtro de syscalls o habilitar
  `MemoryDenyWriteExecute=yes` requiere pruebas en staging con las dependencias
  binarias exactas.

## Bloqueador de runtime

El unit fuente usa:

```text
/usr/bin/python3 -I
```

En el host auditado:

- `/usr/bin/python3`: Python 3.14.6
- runtime validado del proyecto: Python 3.12.13
- contrato del paquete: `>=3.12,<3.13`

Por tanto el unit no puede instalarse sin renderizar `ExecStart` hacia un
runtime Python 3.12 inmutable y root-owned dentro de la release. No se debe usar
un symlink mutable ni relajar `pyproject.toml` para acomodar Python 3.14 sin una
migración separada.

## Gate de cutover

El cutover requiere, en un host staging y con autorización privilegiada:

1. release inmutable, lockfile y Python 3.12 exacto;
2. UIDs/grupo estables mediante `trading-ai-paper.conf`;
3. authz renderizada con UIDs reales, root-owned y modo `0444`;
4. riesgo y universos revisados, root-owned y no escribibles;
5. credenciales entregadas sólo al daemon mediante
   `LoadCredentialEncrypted=`;
6. tests multi-UID, restart/SIGKILL, journal recovery, target/fence stale,
   resultados ambiguos e idempotencia;
7. ausencia de credenciales en monitor y safety;
8. proveedor de riesgo durable server-side antes de permitir aperturas;
9. `open` ausente de la policy hasta que todos los gates anteriores pasen.

## Rollback

1. deshabilitar los tres timers de usuario;
2. no editar manualmente el JSON de riesgo;
3. si existe exposición dudosa, el operador decide el workflow
   `paper-safe-flatten`;
4. restaurar únicamente un snapshot de riesgo con checksum válido;
5. revertir la release por commit y repetir gates;
6. conservar `live_trading_allowed=false`.

