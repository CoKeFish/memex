"""Paso de relaciones DETERMINISTAS del grafo: materializa, sin LLM, las aristas derivables de los
datos ya guardados. Es un paso más del pipeline (on-demand, apagado por default, encadenable por el
daemon): CONSUME lo disponible, no dispara pasos previos. Idempotente.

Produce las clases de arista (ver `memex.relations.edges`):
- PISTAS de co-ocurrencia (`producer='inbox'`, `status='pista'`): dos vértices del MISMO mensaje.
  Señal barata de "quizás se relacionan" — NO asegura relación (el LLM la valida después). Se acota
  el fan-out: un mensaje con demasiados vértices (digest) se SALTA y se loguea.
- REALES de afiliación/pertenencia (`producer='identidades'`, `status='confirmed'`): persona↔org y
  sub→padre que el directorio enlaza explícitamente (dato, no adivinanza).
- REALES de contraparte (`producer='finance'`, `status='confirmed'`): cobro/pago CONSOLIDADO →
  identidad del cobrador/pagador (su `counterparty_identity_id` resuelto). El enlace por identidad
  entre finanzas y el directorio — determinista, el conector más valioso del grafo.

La provenance vértice→mensaje NO es arista (inbox es atributo): vive en `source_inbox_ids`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.relations.edges import (
    PRODUCER_FINANCE,
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

#: Vértices cuyo enlace a inbox es DIRECTO (columna `source_inbox_ids`): slug → tabla. finance ya NO
#: está acá: su vértice es el CONSOLIDADO, cuya procedencia es TRANSITIVA (vía links → crudos).
_DIRECT_SOURCES: tuple[tuple[str, str], ...] = (("hackathones", "mod_hackathones_events"),)


@dataclass(frozen=True)
class RelationStats:
    """Resumen de un paso determinista."""

    cooccurrence_pistas: int
    afiliacion_reales: int
    high_fanout_skipped: int
    pertenencia_reales: int = 0
    contraparte_reales: int = 0


def vertex_inbox_ids(conn: Connection, user_id: int) -> dict[Ref, set[int]]:
    """Mapa vértice → ids de los mensajes (inbox) de los que salió. Directo para hackathones
    (`source_inbox_ids`); TRANSITIVO para finance y calendar (consolidado→crudos) e identidades
    (persona/org ← menciones). Base de la co-ocurrencia (y, luego, del pre-filtro)."""
    prov: dict[Ref, set[int]] = defaultdict(set)

    for slug, table in _DIRECT_SOURCES:
        for r in conn.execute(
            text(f"SELECT id, source_inbox_ids FROM {table} WHERE user_id = :u"), {"u": user_id}
        ).mappings():
            prov[Ref(slug, int(r["id"]))].update(int(x) for x in (r["source_inbox_ids"] or []))

    for r in conn.execute(
        text(
            """
            SELECT l.consolidated_id AS cid, t.source_inbox_ids AS ids
            FROM mod_finance_transaction_links l
            JOIN mod_finance_transactions t ON t.id = l.transaction_id
            JOIN mod_finance_consolidated c ON c.id = l.consolidated_id
            WHERE l.user_id = :u AND NOT c.deleted
            """
        ),
        {"u": user_id},
    ).mappings():
        prov[Ref("finance", int(r["cid"]))].update(int(x) for x in (r["ids"] or []))

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


def _materialize_pertenencia(conn: Connection, user_id: int) -> int:
    """Una arista REAL «pertenece_a» sub→padre por cada `parent_identity_id` del directorio (la
    jerarquía genérica «sub»: programa→universidad, producto→empresa, …). Dirigida (hijo→padre).
    Devuelve cuántas."""
    n = 0
    for r in conn.execute(
        text(
            """
            SELECT c.id AS child_id, c.kind AS child_kind,
                   p.id AS parent_id, p.kind AS parent_kind
            FROM mod_identidades c
            JOIN mod_identidades p ON p.id = c.parent_identity_id
            WHERE c.user_id = :u AND c.parent_identity_id IS NOT NULL
            """
        ),
        {"u": user_id},
    ).mappings():
        child_slug = "identidades:person" if r["child_kind"] == "persona" else "identidades:org"
        parent_slug = "identidades:person" if r["parent_kind"] == "persona" else "identidades:org"
        propose_edge(
            conn,
            user_id,
            Ref(child_slug, int(r["child_id"])),
            Ref(parent_slug, int(r["parent_id"])),
            producer=PRODUCER_IDENTIDADES,
            relation_type="pertenece_a",
            status=STATUS_CONFIRMED,
        )
        n += 1
    return n


def _materialize_contraparte(conn: Connection, user_id: int) -> int:
    """Una arista REAL «contraparte» cobro→identidad por cada transacción CONSOLIDADA cuya
    contraparte resolvió a una identidad del directorio (`counterparty_identity_id`). Dirigida (el
    cobro/pago → quién cobró/pagó). El enlace por identidad finanzas↔directorio."""
    n = 0
    for r in conn.execute(
        text(
            """
            SELECT c.id AS cid, i.id AS iid, i.kind AS kind
            FROM mod_finance_consolidated c
            JOIN mod_identidades i ON i.id = c.counterparty_identity_id
            WHERE c.user_id = :u AND NOT c.deleted AND c.counterparty_identity_id IS NOT NULL
            """
        ),
        {"u": user_id},
    ).mappings():
        slug = "identidades:person" if r["kind"] == "persona" else "identidades:org"
        propose_edge(
            conn,
            user_id,
            Ref("finance", int(r["cid"])),
            Ref(slug, int(r["iid"])),
            producer=PRODUCER_FINANCE,
            relation_type="contraparte",
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
    pert = _materialize_pertenencia(conn, user_id)
    contraparte = _materialize_contraparte(conn, user_id)
    stats = RelationStats(
        cooccurrence_pistas=pistas,
        afiliacion_reales=afil,
        high_fanout_skipped=skipped,
        pertenencia_reales=pert,
        contraparte_reales=contraparte,
    )
    _log.info(
        "relation.build.done",
        user_id=user_id,
        cooccurrence_pistas=pistas,
        afiliacion_reales=afil,
        pertenencia_reales=pert,
        contraparte_reales=contraparte,
        high_fanout_skipped=skipped,
    )
    return stats
