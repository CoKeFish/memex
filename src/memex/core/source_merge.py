"""Fusionar dos fuentes del mismo usuario y tipo: `memex-sources merge --from A --into B`.

Cuándo hace falta: una cuenta se borró y se creó de nuevo (o hay dos cuentas en los mismos chats) y
el mismo historial entró dos veces, una por fuente. La fuente vieja quedó huérfana (sin cuenta) pero
con todo lo ya extraído; la nueva es la viva. Fusionar deja UNA fuente (la de destino) con:

- las filas que solo estaban en el origen, re-apuntadas al destino;
- por cada mensaje repetido (misma identidad en ambas), la fila MÁS VIEJA (la que ya tiene
  extracciones) y, sumados, los archivos que la otra copia hubiera bajado; la copia nueva se borra.
  Una fila que gana archivos vuelve a la cola de extracción (hay algo nuevo que leer);
- el origen borrado (sus corridas/cursores se van con él: ON DELETE CASCADE).

Solo para tipos cuyo `external_id` es identidad del mensaje (chats): en correo el external_id es
relativo a la carpeta/cuenta y no sirve para emparejar. Dry-run por default.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.inbox import invalidate_for_reprocess
from memex.db import connection
from memex.logging import get_logger, setup_logging

_log = get_logger("memex.core.source_merge")

#: Tipos cuyo `external_id` identifica al mensaje con independencia de la cuenta que lo recibió.
MERGEABLE_TYPES = frozenset({"whatsapp", "telegram"})


class SourceMergeError(ValueError):
    pass


@dataclass(frozen=True)
class MergeStats:
    source_from: int
    source_into: int
    #: mensajes repetidos en ambas fuentes (se conserva la copia más vieja)
    duplicates: int
    #: archivos que la copia descartada había bajado y la conservada no tenía
    media_moved: int
    #: filas conservadas que volvieron a la cola de extracción por ganar archivos
    requeued: int
    #: filas del origen que no estaban en el destino y pasaron a él
    moved: int
    applied: bool


def _source(conn: Connection, source_id: int) -> tuple[int, str]:
    row = conn.execute(
        text("SELECT user_id, type FROM sources WHERE id = :s"), {"s": source_id}
    ).first()
    if row is None:
        raise SourceMergeError(f"la fuente {source_id} no existe")
    return int(row[0]), str(row[1])


def merge_sources(
    conn: Connection, source_from: int, source_into: int, *, apply: bool
) -> MergeStats:
    if source_from == source_into:
        raise SourceMergeError("origen y destino son la misma fuente")
    user_from, type_from = _source(conn, source_from)
    user_into, type_into = _source(conn, source_into)
    if user_from != user_into:
        raise SourceMergeError("las fuentes son de usuarios distintos")
    if type_from != type_into:
        raise SourceMergeError(f"tipos distintos: {type_from!r} vs {type_into!r}")
    if type_from not in MERGEABLE_TYPES:
        raise SourceMergeError(
            f"el tipo {type_from!r} no se fusiona por external_id (solo {sorted(MERGEABLE_TYPES)})"
        )
    user_id = user_from

    pairs = conn.execute(
        text(
            """
            SELECT LEAST(a.id, b.id) AS keep, GREATEST(a.id, b.id) AS drop
              FROM inbox a JOIN inbox b ON b.external_id = a.external_id
             WHERE a.source_id = :f AND b.source_id = :i
             ORDER BY 1
            """
        ),
        {"f": source_from, "i": source_into},
    ).all()
    moved = int(
        conn.execute(
            text(
                """
                SELECT count(*) FROM inbox a
                 WHERE a.source_id = :f
                   AND NOT EXISTS (SELECT 1 FROM inbox b
                                    WHERE b.source_id = :i AND b.external_id = a.external_id)
                """
            ),
            {"f": source_from, "i": source_into},
        ).scalar_one()
    )
    media_moved = requeued = 0
    for keep, drop in pairs:
        keep_id, drop_id = int(keep), int(drop)
        gained = conn.execute(
            text(
                """
                SELECT count(*) FROM media_assets d
                 WHERE d.inbox_id = :d
                   AND NOT EXISTS (SELECT 1 FROM media_assets k
                                    WHERE k.inbox_id = :k AND k.sha256 = d.sha256)
                """
            ),
            {"d": drop_id, "k": keep_id},
        ).scalar_one()
        gained = int(gained)
        if apply:
            if gained:
                conn.execute(
                    text(
                        """
                        UPDATE media_assets d SET inbox_id = :k
                         WHERE d.inbox_id = :d
                           AND NOT EXISTS (SELECT 1 FROM media_assets k
                                            WHERE k.inbox_id = :k AND k.sha256 = d.sha256)
                        """
                    ),
                    {"d": drop_id, "k": keep_id},
                )
                invalidate_for_reprocess(conn, user_id, [keep_id])
            conn.execute(text("DELETE FROM inbox WHERE id = :d"), {"d": drop_id})
        media_moved += gained
        requeued += 1 if gained else 0

    if apply:
        # Tras borrar las copias repetidas, lo que queda en el origen no choca con el destino.
        conn.execute(
            text("UPDATE inbox SET source_id = :i WHERE source_id = :f"),
            {"f": source_from, "i": source_into},
        )
        conn.execute(
            text("UPDATE inbox_dedupe_keys SET source_id = :i WHERE source_id = :f"),
            {"f": source_from, "i": source_into},
        )
        conn.execute(text("DELETE FROM sources WHERE id = :f"), {"f": source_from})

    stats = MergeStats(
        source_from=source_from,
        source_into=source_into,
        duplicates=len(pairs),
        media_moved=media_moved,
        requeued=requeued,
        moved=moved,
        applied=apply,
    )
    _log.info("sources.merge", user_id=user_id, **asdict(stats))
    return stats


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-sources", description="Operaciones sobre fuentes.")
    sp = p.add_subparsers(dest="cmd", required=True)
    m = sp.add_parser(
        "merge",
        help="Fusiona la fuente --from dentro de --into (mismo usuario y tipo) y borra el origen.",
    )
    m.add_argument("--from", dest="source_from", type=int, required=True)
    m.add_argument("--into", dest="source_into", type=int, required=True)
    m.add_argument("--apply", action="store_true", help="sin esto es dry-run")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    args = _build_parser().parse_args(argv)
    try:
        with connection() as conn:
            stats = merge_sources(conn, args.source_from, args.source_into, apply=args.apply)
    except SourceMergeError as e:
        print(f"error: {e}")  # noqa: T201 - CLI
        return 2
    mode = "APLICADO" if stats.applied else "dry-run (agregá --apply)"
    print(  # noqa: T201 - CLI
        f"{mode}: {stats.duplicates} repetidos (se conserva la copia vieja), "
        f"{stats.media_moved} archivos sumados, {stats.requeued} filas de vuelta a extracción, "
        f"{stats.moved} filas movidas de {stats.source_from} a {stats.source_into}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
