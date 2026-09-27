"""Salud REAL de una cuenta: probar las credenciales contra el proveedor y decir qué tan fresca es
la evidencia.

Antes la tarjeta de /cuenta decía «OK» por la última ingesta buena, aunque fuera de hace una
semana y el token OAuth ya estuviera caducado: nadie volvía a probarlo hasta que el usuario tocaba
«Validar». Reglas:

- **Evidencia**: el health-check más reciente (manual, del connect, o el automático del daemon) o
  la última corrida de ingesta terminada, lo que sea más nuevo (`effective_health`).
- **Caducidad**: evidencia de más de `FRESH_HOURS` se reporta `stale` — la UI no la pinta en
  verde, la pinta «sin verificar» y dispara una validación; el productor `verificar_cuentas` del
  daemon de notificaciones hace lo mismo cada `cada_horas` sin que nadie abra el dashboard.
- **Fallo visible**: una cuenta `unhealthy` es una alerta de sistema (`memex.core.alerts`), así
  suena la campana del dashboard y llega el aviso por el canal configurado.

`check_account` es el ÚNICO camino que ejecuta un `health_check()` y persiste el resultado; lo
usan el endpoint `POST /accounts/{id}/health-check` y el daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.source import HealthResult
from memex.db import connection
from memex.logging import get_logger

_log = get_logger("memex.core.account_health")

#: Evidencia (chequeo o ingesta) más vieja que esto ya no sostiene un «OK» en verde.
FRESH_HOURS = 24


@dataclass(frozen=True)
class EffectiveHealth:
    status: str
    #: `check` (manda el health-check), `ingestion` (manda la ingesta) o `none` (nada aún).
    basis: str
    #: Instante de la evidencia que decide; None si no hay ninguna.
    evidence_at: datetime | None
    #: True si hay evidencia pero es más vieja que `FRESH_HOURS` (o no hay ninguna).
    stale: bool


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def effective_health(
    conn: Connection,
    account_id: int,
    stored: str,
    last_check_at: datetime | None,
    *,
    now: datetime | None = None,
    fresh_hours: int = FRESH_HOURS,
) -> EffectiveHealth:
    """Salud que REFLEJA la realidad y dice de dónde sale y qué tan fresca es.

    Dos evidencias compiten: el último `ingestion_run` terminado de las sources de la cuenta
    (ok→healthy, failed/aborted→unhealthy) y el último health-check. Gana la MÁS RECIENTE (un run
    `ok` de meses atrás no tapa un chequeo `unhealthy` de hace un minuto, y una fuente que ACABA de
    ingestar bien no aparece 'unhealthy' por un chequeo viejo). `stale` marca la evidencia vencida.
    """
    run = (
        conn.execute(
            text(
                """
                SELECT ir.status, ir.started_at FROM ingestion_runs ir
                JOIN sources s ON s.id = ir.source_id
                WHERE s.account_id = :aid AND ir.status IN ('ok', 'failed', 'aborted')
                ORDER BY ir.started_at DESC
                LIMIT 1
                """
            ),
            {"aid": account_id},
        )
        .mappings()
        .first()
    )
    check_at = _aware(last_check_at)
    run_at = _aware(run["started_at"]) if run is not None else None
    at: datetime | None
    if check_at is not None and (run_at is None or check_at >= run_at):
        status, basis, at = stored, "check", check_at
    elif run is not None:
        status = "healthy" if run["status"] == "ok" else "unhealthy"
        basis, at = "ingestion", run_at
    else:
        status, basis, at = stored, "none", None
    ref = now or datetime.now(UTC)
    stale = at is None or (ref - at) > timedelta(hours=fresh_hours)
    return EffectiveHealth(status=status, basis=basis, evidence_at=at, stale=stale)


def linked_source(conn: Connection, account_id: int) -> tuple[str, dict[str, Any]] | None:
    """Tipo + config de la primera source vinculada a la cuenta (para resolver env / salud)."""
    src = (
        conn.execute(
            text("SELECT type, config FROM sources WHERE account_id = :aid ORDER BY id LIMIT 1"),
            {"aid": account_id},
        )
        .mappings()
        .first()
    )
    if src is None:
        return None
    return str(src["type"]), dict(src["config"] or {})


async def check_account(account_id: int, user_id: int) -> HealthResult | None:
    """Instancia el Source vinculado con las creds descifradas, corre `health_check()` y persiste el
    estado (`health_status`, `last_health_check_at`, `health_detail`, `health_code`). None si la
    cuenta no tiene source vinculada. Asume ownership ya verificado. Levanta KeyError (tipo sin
    ingestor) / SourceConfigError (config inválida); un fallo del proveedor NO levanta: queda como
    `unhealthy` con su detalle."""
    # Import tardío: el registro de sources arrastra los ingestores (core no depende de ellos
    # al importarse; solo al ejecutar un chequeo).
    from memex import sources as source_registry
    from memex.sources.resolver import build_resolved_env

    with connection() as conn:
        src = linked_source(conn, account_id)
        if src is None:
            return None
        source_type, cfg = src
        device_check = source_registry.resolve_device_health(source_type)
        resolved_env = (
            None
            if device_check is not None
            else build_resolved_env(
                conn, user_id=user_id, source_type=source_type, cfg=cfg, account_id=account_id
            )
        )

    if device_check is not None:
        # Fuente push (dispositivo vinculado): no hay credenciales que probar; la salud es el
        # estado del vínculo y la conexión viva.
        result = await device_check(account_id)
    else:
        factory = source_registry.resolve(source_type)
        source = factory(cfg, env=resolved_env)
        result = await source.health_check()

    with connection() as conn:
        conn.execute(
            text(
                "UPDATE accounts SET health_status = :s, last_health_check_at = NOW(), "
                "health_detail = :d, health_code = :c WHERE id = :aid"
            ),
            {"s": result.status, "d": result.detail, "c": result.code, "aid": account_id},
        )
    _log.info(
        "accounts.health_check",
        user_id=user_id,
        account_id=account_id,
        status=result.status,
        code=result.code,
    )
    return result


def accounts_due_for_check(
    conn: Connection, user_id: int, *, older_than_hours: int, now: datetime | None = None
) -> list[int]:
    """Cuentas habilitadas CON source vinculada cuya evidencia de salud es más vieja que
    `older_than_hours` (o inexistente). Es lo que el daemon revalida solo."""
    rows = (
        conn.execute(
            text(
                """
                SELECT a.id, a.health_status, a.last_health_check_at
                FROM accounts a
                WHERE a.user_id = :uid AND a.enabled
                  AND EXISTS (SELECT 1 FROM sources s WHERE s.account_id = a.id)
                ORDER BY a.id
                """
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    out: list[int] = []
    for r in rows:
        eh = effective_health(
            conn,
            int(r["id"]),
            str(r["health_status"]),
            r["last_health_check_at"],
            now=now,
            fresh_hours=older_than_hours,
        )
        if eh.stale:
            out.append(int(r["id"]))
    return out
