"""Reparación de fechas de mensajes ya ingeridos con un desfase FIJO de zona horaria.

Caso que la motiva (2026-09-06): el plugin `outlook-desktop` tomaba el reloj COM de Outlook (hora
LOCAL etiquetada como UTC por pywin32) como si fuera UTC → todo correo de esa fuente quedó con
`occurred_at` y `payload.date` desplazados por el offset local (en Bogotá, 5 h antes). El plugin
ya está corregido; esto repone lo histórico de forma AUTOMATIZADA e IDEMPOTENTE (marca cada fila
reparada en `payload._repairs`), sin UPDATE a mano.

CLI: `memex-inbox-repair shift --source-id 2 --hours 5 --before 2026-09-06T23:00:00Z [--apply]`
(sin `--apply` es dry-run). Solo fuentes de tipo `outlook`, salvo `--force`.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.logging import get_logger, setup_logging

_log = get_logger("memex.core.inbox_time_repair")
REPAIR_KIND = "tz_shift"


@dataclass(frozen=True)
class ShiftStats:
    candidates: int
    shifted: int
    already_repaired: int
    sample: tuple[tuple[int, str, str], ...]  # (inbox_id, antes, después)


def _shift_iso(value: Any, delta: timedelta) -> Any:
    """Desplaza un ISO-8601 del payload conservando el formato `...Z`; lo que no parsea se deja."""
    if not isinstance(value, str) or not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    out = (dt + delta).astimezone(UTC).isoformat()
    return out.replace("+00:00", "Z")


def shift_source_times(
    conn: Connection,
    user_id: int,
    *,
    source_id: int,
    hours: float,
    before: datetime,
    apply: bool,
    force: bool = False,
) -> ShiftStats:
    """Suma `hours` a `occurred_at` y `payload.date` de los mensajes de `source_id` con
    `occurred_at < before` que no tengan ya una reparación `tz_shift`. Devuelve estadísticas;
    con `apply=False` no escribe nada."""
    src = conn.execute(
        text("SELECT type FROM sources WHERE id = :s AND user_id = :u"),
        {"s": source_id, "u": user_id},
    ).scalar()
    if src is None:
        raise ValueError(f"source {source_id} no existe para el usuario {user_id}")
    if src != "outlook" and not force:
        raise ValueError(f"source {source_id} es de tipo {src!r}, no outlook (usa --force)")
    delta = timedelta(hours=hours)
    rows = (
        conn.execute(
            text(
                """
            SELECT id, occurred_at, payload->>'date' AS date,
                   COALESCE(payload->'_repairs', '[]'::jsonb) AS repairs
            FROM inbox
            WHERE user_id = :u AND source_id = :s AND occurred_at < :before
            ORDER BY id
            """
            ),
            {"u": user_id, "s": source_id, "before": before},
        )
        .mappings()
        .all()
    )
    shifted = 0
    already = 0
    sample: list[tuple[int, str, str]] = []
    for r in rows:
        repairs = r["repairs"] if isinstance(r["repairs"], list) else []
        if any(isinstance(x, dict) and x.get("kind") == REPAIR_KIND for x in repairs):
            already += 1
            continue
        new_at = r["occurred_at"] + delta
        if len(sample) < 5:
            sample.append((int(r["id"]), str(r["occurred_at"]), str(new_at)))
        if apply:
            mark = {
                "kind": REPAIR_KIND,
                "hours": hours,
                "at": datetime.now(UTC).isoformat(),
                "occurred_at_before": str(r["occurred_at"]),
            }
            conn.execute(
                text(
                    """
                    UPDATE inbox
                       SET occurred_at = occurred_at + make_interval(secs => :secs),
                           payload = jsonb_set(
                               jsonb_set(payload, '{_repairs}',
                                         COALESCE(payload->'_repairs', '[]'::jsonb)
                                             || CAST(:mark AS JSONB)),
                               '{date}', CAST(:date AS JSONB), TRUE)
                     WHERE id = :id
                    """
                ),
                {
                    "secs": delta.total_seconds(),
                    "mark": json.dumps(mark),
                    "date": json.dumps(_shift_iso(r["date"], delta)),
                    "id": int(r["id"]),
                },
            )
        shifted += 1
    _log.info(
        "inbox.time_repair.shift",
        user_id=user_id,
        source_id=source_id,
        hours=hours,
        before=before.isoformat(),
        apply=apply,
        candidates=len(rows),
        shifted=shifted,
        already_repaired=already,
    )
    return ShiftStats(len(rows), shifted, already, tuple(sample))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memex-inbox-repair", description="Reparaciones de fechas de mensajes ingeridos"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("shift", help="suma N horas a occurred_at/payload.date de una fuente")
    sp.add_argument("--user", type=int, default=1)
    sp.add_argument("--source-id", type=int, required=True)
    sp.add_argument("--hours", type=float, required=True)
    sp.add_argument("--before", required=True, help="ISO-8601: solo mensajes anteriores a esto")
    sp.add_argument("--apply", action="store_true", help="sin esto es dry-run")
    sp.add_argument("--force", action="store_true", help="permite fuentes que no sean outlook")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    args = _build_parser().parse_args(argv)
    before = datetime.fromisoformat(args.before.replace("Z", "+00:00"))
    if before.tzinfo is None:
        before = before.replace(tzinfo=UTC)
    with connection() as conn:
        st = shift_source_times(
            conn,
            args.user,
            source_id=args.source_id,
            hours=args.hours,
            before=before,
            apply=args.apply,
            force=args.force,
        )
    mode = "APLICADO" if args.apply else "dry-run"
    print(  # noqa: T201 — CLI
        f"{mode}: candidatos={st.candidates} desplazados={st.shifted} "
        f"ya_reparados={st.already_repaired}"
    )
    for iid, a, b in st.sample:
        print(f"  #{iid}: {a} → {b}")  # noqa: T201 — CLI
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
