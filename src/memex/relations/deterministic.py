"""Paso de relaciones DETERMINISTAS del grafo: materializa, sin LLM, las aristas derivables de los
datos ya guardados. Es un paso más del pipeline (on-demand, apagado por default, encadenable por el
daemon): CONSUME lo disponible, no dispara pasos previos. Idempotente.

Produce las dos clases de arista (ver `memex.relations.edges`):
- PISTAS de co-ocurrencia (`producer='inbox'`, `status='pista'`): dos vértices del MISMO mensaje.
  Señal barata de "quizás se relacionan" — NO asegura relación (el LLM la valida después). Se acota
  el fan-out: un mensaje con demasiados vértices (digest) se SALTA y se loguea.
- REALES de afiliación (`producer='identidades'`, `status='confirmed'`): persona↔org que el
  directorio enlaza explícitamente (dato, no adivinanza).

La provenance vértice→mensaje NO es arista (inbox es atributo): vive en `source_inbox_ids`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.relations.edges import (
    PRODUCER_IDENTIDADES,
    PRODUCER_INBOX,
    STATUS_CONFIRMED,
    Ref,
    propose_edge,
)

_log = get_logger("memex.relations.deterministic")

#: Tope de vértices por mensaje para emitir co-ocurrencia. Un mensaje con más (digest/newsletter) se
#: salta: ahí la co-ocurrencia es ruido (C(n,2) aristas sin sentido). Los saltados se loguean.
DEFAULT_COOCCURRENCE_CAP = 8

#: Vértices cuyo enlace a inbox es DIRECTO (columna `source_inbox_ids`): slug → tabla.
_DIRECT_SOURCES: tuple[tuple[str, str], ...] = (
    ("finance", "mod_finance_expenses"),
    ("hackathones", "mod_hackathones_events"),
)


@dataclass(frozen=True)
class RelationStats:
    """Resumen de un paso determinista."""

    cooccurrence_pistas: int
    afiliacion_reales: int
    high_fanout_skipped: int


def vertex_inbox_ids(conn: Connection, user_id: int) -> dict[Ref, set[int]]:
    """Mapa vértice → ids de los mensajes (inbox) de los que salió. Directo para finance/hackathones
    (`source_inbox_ids`); TRANSITIVO para calendar (consolidado→crudos) e identidades (persona/org ←
    menciones). Base de la co-ocurrencia (y, luego, del pre-filtro)."""
    prov: dict[Ref, set[int]] = defaultdict(set)

    for slug, table in _DIRECT_SOURCES:
        for r in conn.execute(
            text(f"SELECT id, source_inbox_ids FROM {table} WHERE user_id = :u"), {"u": user_id}
        ).mappings():
            prov[Ref(slug, int(r["id"]))].update(int(x) for x in (r["source_inbox_ids"] or []))

    for r in conn.execute(
        text(
            """
            SELECT l.consolidated_id AS cid, e.source_inbox_ids AS ids
            FROM mod_calendar_event_links l
            JOIN mod_calendar_events e ON e.id = l.event_id
            JOIN mod_calendar_consolidated c ON c.id = l.consolidated_id
            WHERE l.user_id = :u AND NOT c.deleted
            """
        ),
        {"u": user_id},
    ).mappings():
        prov[Ref("calendar", int(r["cid"]))].update(int(x) for x in (r["ids"] or []))

    for r in conn.execute(
        text(
            """
            SELECT m.resolved_identity_id AS iid, i.kind AS kind, m.source_inbox_ids AS ids
            FROM mod_identidades_mentions m
            JOIN mod_identidades i ON i.id = m.resolved_identity_id
            WHERE m.user_id = :u AND m.resolved_identity_id IS NOT NULL
            """
        ),
        {"u": user_id},
    ).mappings():
        ids = [int(x) for x in (r["ids"] or [])]
        slug = "identidades:person" if r["kind"] == "persona" else "identidades:org"
        prov[Ref(slug, int(r["iid"]))].update(ids)

    return dict(prov)


def _materialize_cooccurrence(
    conn: Connection, user_id: int, prov: dict[Ref, set[int]], cap: int
) -> tuple[int, int]:
    """Por cada mensaje, una arista-pista entre cada par de vértices que salieron de él (par
    canónico ordenado). Salta mensajes con más de `cap` vértices. Devuelve (pistas, saltados)."""
    by_msg: dict[int, set[Ref]] = defaultdict(set)
    for ref, ids in prov.items():
        for mid in ids:
            by_msg[mid].add(ref)

    pistas = 0
    skipped = 0
    seen: set[tuple[Ref, Ref]] = set()
    for mid, refs in by_msg.items():
        uniq = sorted(refs, key=lambda r: (r.slug, r.id))
        if len(uniq) < 2:
            continue
        if len(uniq) > cap:
            skipped += 1
            _log.info("relation.cooccurrence.skip_high_fanout", inbox_id=mid, vertices=len(uniq))
            continue
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                pair = (uniq[i], uniq[j])
                if pair in seen:
                    continue
                seen.add(pair)
                propose_edge(
                    conn,
                    user_id,
                    uniq[i],
                    uniq[j],
                    producer=PRODUCER_INBOX,
                    relation_type="co-ocurrencia",
                    evidence=f"inbox:{mid}",
                )
                pistas += 1
    return pistas, skipped


def _materialize_afiliacion(conn: Connection, user_id: int) -> int:
    """Una arista REAL persona→org por cada enlace explícito del directorio. Devuelve cuántas."""
    n = 0
    for r in conn.execute(
        text("SELECT person_id, org_id FROM mod_identidades_person_orgs WHERE user_id = :u"),
        {"u": user_id},
    ).mappings():
        propose_edge(
            conn,
            user_id,
            Ref("identidades:person", int(r["person_id"])),
            Ref("identidades:org", int(r["org_id"])),
            producer=PRODUCER_IDENTIDADES,
            relation_type="afiliado",
            status=STATUS_CONFIRMED,
        )
        n += 1
    return n


def build_relations(
    conn: Connection, user_id: int, *, cooccurrence_cap: int = DEFAULT_COOCCURRENCE_CAP
) -> RelationStats:
    """Materializa las aristas deterministas del user (idempotente). Consume lo disponible; NO
    dispara extracción/consolidación. Devuelve el resumen."""
    prov = vertex_inbox_ids(conn, user_id)
    pistas, skipped = _materialize_cooccurrence(conn, user_id, prov, cooccurrence_cap)
    afil = _materialize_afiliacion(conn, user_id)
    stats = RelationStats(
        cooccurrence_pistas=pistas, afiliacion_reales=afil, high_fanout_skipped=skipped
    )
    _log.info(
        "relation.build.done",
        user_id=user_id,
        cooccurrence_pistas=pistas,
        afiliacion_reales=afil,
        high_fanout_skipped=skipped,
    )
    return stats
