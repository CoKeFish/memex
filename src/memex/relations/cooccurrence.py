"""Generación de las PISTAS de co-ocurrencia del grafo (paso 7 del pipeline, determinista e
idempotente). Por cada mensaje, una arista-pista entre cada par de vértices que salieron de él:
señal barata de "quizás se relacionan" que el juicio por-mensaje (`relations.per_message`) valida.

Una co-ocurrencia nace `extracted+ambiguous` (la co-aparición es un hecho; la relación, sospecha sin
juzgar). Se acota el fan-out (un mensaje con demasiados vértices —digest— se SALTA y se loguea); un
par que YA tiene arista confirmada (cualquier producer/relation_type/orientación) se suprime —y la
pista redundante pre-existente se CONFIRMA con decisión `regla/'redundante'` (historial, no DELETE)—
porque no aporta sobre la relación vouchada; el grafo de clusterización salta la co-ocurrencia de
esos pares para no doble-contar el peso. La PROCEDENCIA de cada pista se acumula en
`relation_edge_sources` (TODOS los mensajes donde el par co-ocurrió, no solo el primero).

La provenance vértice→mensaje (`vertex_inbox_ids`) NO es arista (inbox es atributo): vive en
`source_inbox_ids`, en las MENCIONES (incluido el REMITENTE, persistido en el paso 5 como
avistamiento 'sender' por `modules/identidades/senders.py` — ya NO se deriva al vuelo) o se DERIVA
del payload (solo el CANAL). Es la base de la co-ocurrencia y la consume también el drill-down del
API (`/graph`) y la timeline de cúmulos.

Antes esto vivía en el barrido global `build_relations`; ahora `generate_cooccurrence` es la primera
parte de la fase de co-ocurrencia, que luego juzga (`run_per_message_confirm`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import settings
from memex.core.source import SourceKind
from memex.logging import get_logger
from memex.relations.decisions import (
    METHOD_REGLA,
    VERDICT_CONFIRM,
    add_edge_sources,
    edge_sources,
    evidence_signature,
    record_decision,
)
from memex.relations.edges import (
    CANAL_SLUG,
    PRODUCER_INBOX,
    PROVENANCE_EXTRACTED,
    RELTYPE_COOCURRENCIA,
    VERDICT_AMBIGUOUS,
    VERDICT_CONFIRMED,
    Ref,
    list_edges,
    mark_vertices_dirty,
    propose_edge,
    resolve_edge,
)
from memex.relations.vertices import IDENTITY_SLUG_BY_KIND
from memex.sources import kind_for_type

_log = get_logger("memex.relations.cooccurrence")

#: Tope de vértices por mensaje para emitir co-ocurrencia. Un mensaje con más (digest/newsletter) se
#: salta: ahí la co-ocurrencia es ruido (C(n,2) aristas sin sentido). Los saltados se loguean.
DEFAULT_COOCCURRENCE_CAP = 8

#: Vértices cuyo enlace a inbox es DIRECTO (columna `source_inbox_ids`): slug → tabla. finance ya NO
#: está acá: su vértice es el CONSOLIDADO, cuya procedencia es TRANSITIVA (vía links → crudos).
_DIRECT_SOURCES: tuple[tuple[str, str], ...] = (("hackathones", "mod_hackathones_events"),)


def vertex_inbox_ids(conn: Connection, user_id: int) -> dict[Ref, set[int]]:
    """Mapa vértice → ids de los mensajes (inbox) de los que salió. Directo para hackathones
    (`source_inbox_ids`); TRANSITIVO para finance y calendar (consolidado→crudos) e identidades
    (persona/org/producto ← menciones, INCLUIDO el remitente, persistido en el paso 5 como
    avistamiento 'sender'); DERIVADO solo para el CANAL (payload→canal). Base de la co-ocurrencia
    (y, luego, del pre-filtro)."""
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
        slug = IDENTITY_SLUG_BY_KIND[str(r["kind"])]
        prov[Ref(slug, int(r["iid"]))].update(ids)

    # CANAL (derivado): el canal está en TODO mensaje de su chat → co-ocurre con lo que salga de
    # cada uno. NO cuenta para el cap (es estructural, ver `_materialize_cooccurrence`).
    for r in conn.execute(
        text(
            """
            SELECT c.id AS cid, i.id AS mid
            FROM mod_canales c
            JOIN inbox i ON i.user_id = c.user_id AND i.payload->>'chat_id' = c.external_id
            WHERE c.user_id = :u AND i.payload->>'chat_id' IS NOT NULL
            """
        ),
        {"u": user_id},
    ).mappings():
        prov[Ref(CANAL_SLUG, int(r["cid"]))].add(int(r["mid"]))

    return dict(prov)


def _eff_cap(mid: int, chat_ids: set[int], cap: int) -> int:
    """Cap efectivo de un mensaje: el chat usa el MÍNIMO entre `cap` y `chat_cooccurrence_cap`
    (genera muchas co-ocurrencias y aporta menos). Único punto compartido por el salto de
    `_materialize_cooccurrence` y la detección de densos de `dense_message_vertices`: que el
    predicado de densidad NO pueda divergir entre el que SALTA y el que PROPONE."""
    return min(cap, settings.chat_cooccurrence_cap) if mid in chat_ids else cap


def _materialize_cooccurrence(
    conn: Connection,
    user_id: int,
    prov: dict[Ref, set[int]],
    cap: int,
    confirmed_pairs: set[frozenset[Ref]],
) -> tuple[int, int]:
    """Por cada mensaje, una arista-pista entre cada par de vértices que salieron de él (par
    canónico ordenado). Salta mensajes con más de `cap` vértices de CONTENIDO y los pares de
    `confirmed_pairs` (ya hay arista confirmada entre ambos, cualquier orientación): la pista no
    aporta sobre una relación vouchada y, promovida por la cascada, doble-contaría el peso del par
    en la clusterización. El CANAL no cuenta para el cap (es estructural y fijo en todo mensaje de
    chat; contarlo acercaría injustamente los chats al tope — el remitente SÍ cuenta, es contenido
    real); los pares de un mensaje no saltado se emiten canal incluido. PROCEDENCIA: cada
    par-mensaje se acumula en `relation_edge_sources` (también para aristas ya terminales — el
    `evidence='inbox:N'` conserva solo el primero por idempotencia, esta tabla los conserva
    TODOS). Un mensaje saltado pierde sus pistas; `dense_message_vertices` lo expone y la fase de
    PROPUESTA de `relations.per_message` le pide al LLM sus pares relacionados ALL-TYPE (reemplazó
    al relevo solo-identidad). Devuelve (pistas, saltados)."""
    by_msg: dict[int, set[Ref]] = defaultdict(set)
    for ref, ids in prov.items():
        for mid in ids:
            by_msg[mid].add(ref)

    # Reglas para chats: generan muchas co-ocurrencias y suelen aportar menos → cap más bajo
    # (`_eff_cap`, MÍNIMO entre el cap general y `chat_cooccurrence_cap`), así nacen menos pares.
    chat_ids = _chat_message_ids(conn, user_id, by_msg.keys())

    # Aristas cooc ya materializadas (cualquier status): la procedencia sigue creciendo sobre
    # ellas sin re-proponer. El par canónico (slug, id) coincide con cómo se crearon.
    existing: dict[tuple[Ref, Ref], int] = {
        (e.src, e.dst): e.id
        for e in list_edges(conn, user_id, producer=PRODUCER_INBOX)
        if e.relation_type == RELTYPE_COOCURRENCIA
    }

    pistas = 0
    skipped = 0
    counted: set[tuple[Ref, Ref]] = set()
    new_refs: set[Ref] = set()  # vértices de pares NUEVOS → dirty (groundwork incremental ADR-021)
    sources_by_edge: dict[int, set[int]] = defaultdict(set)
    for mid, refs in by_msg.items():
        uniq = sorted(refs, key=lambda r: (r.slug, r.id))
        if len(uniq) < 2:
            continue
        content = sum(1 for r in uniq if r.slug != CANAL_SLUG)
        eff_cap = _eff_cap(mid, chat_ids, cap)
        if content > eff_cap:
            skipped += 1
            _log.info(
                "relation.cooccurrence.skip_high_fanout",
                inbox_id=mid,
                vertices=content,
                cap=eff_cap,
                chat=mid in chat_ids,
            )
            continue
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                pair = (uniq[i], uniq[j])
                eid = existing.get(pair)
                if frozenset(pair) in confirmed_pairs:
                    # Par ya vouchado: no se propone ni cuenta; si la cooc quedó materializada
                    # (confirmada por redundancia/partidor, o aún pista) su procedencia crece.
                    if eid is not None:
                        sources_by_edge[eid].add(mid)
                    continue
                if eid is None:
                    eid = propose_edge(
                        conn,
                        user_id,
                        uniq[i],
                        uniq[j],
                        producer=PRODUCER_INBOX,
                        relation_type=RELTYPE_COOCURRENCIA,
                        evidence=f"inbox:{mid}",
                    )
                    existing[pair] = eid
                    new_refs.update(pair)
                if pair not in counted:
                    counted.add(pair)
                    pistas += 1
                sources_by_edge[eid].add(mid)
    for eid, mids in sources_by_edge.items():
        add_edge_sources(conn, eid, mids)
    if new_refs:
        mark_vertices_dirty(conn, user_id, sorted(new_refs, key=lambda r: (r.slug, r.id)))
    return pistas, skipped


def _chat_message_ids(conn: Connection, user_id: int, inbox_ids: Iterable[int]) -> set[int]:
    """Los inbox_ids que son de un medio de CHAT (su `sources.type` mapea a `SourceKind.CHAT`)."""
    ids = sorted(set(inbox_ids))
    if not ids:
        return set()
    out: set[int] = set()
    for r in conn.execute(
        text(
            "SELECT i.id AS id, s.type AS type FROM inbox i JOIN sources s ON s.id = i.source_id "
            "WHERE i.user_id = :u AND i.id = ANY(:ids)"
        ),
        {"u": user_id, "ids": ids},
    ).mappings():
        try:
            if kind_for_type(str(r["type"])) == SourceKind.CHAT:
                out.add(int(r["id"]))
        except KeyError:
            pass
    return out


def _resolve_redundant_cooccurrence(
    conn: Connection, user_id: int, confirmed_pairs: set[frozenset[Ref]]
) -> int:
    """CONFIRMA (sin borrar: historial) las pistas de co-ocurrencia que quedaron REDUNDANTES: ya
    existe una arista confirmada entre los mismos dos vértices (cualquier producer/relation_type/
    orientación) — el dato real vouchó la relación y la pista hereda el veredicto, con decisión
    `regla/'redundante'` y su procedencia intacta. El peso del par no se doble-cuenta: el grafo de
    clusterización salta la co-ocurrencia de pares con real confirmada (`build_cluster_graph`).
    Mismo triple-filtro (pista + inbox + co-ocurrencia) que la cascada del partidor: las ya
    terminales NO se tocan. Devuelve cuántas confirmó."""
    redundant = [
        e
        for e in list_edges(conn, user_id, verdict=VERDICT_AMBIGUOUS, producer=PRODUCER_INBOX)
        if e.relation_type == RELTYPE_COOCURRENCIA and frozenset((e.src, e.dst)) in confirmed_pairs
    ]
    if not redundant:
        return 0
    sources = edge_sources(conn, [e.id for e in redundant])
    n = 0
    for e in redundant:
        if resolve_edge(
            conn,
            e.id,
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
            relation="ya existe una relación confirmada entre ambos",
        ):
            record_decision(
                conn,
                user_id,
                e.id,
                verdict=VERDICT_CONFIRM,
                method=METHOD_REGLA,
                rule="redundante",
                evidence_sig=evidence_signature(sources.get(e.id, set())),
            )
            mark_vertices_dirty(conn, user_id, [e.src, e.dst])  # deja el delta completo
            n += 1
    return n


def generate_cooccurrence(
    conn: Connection, user_id: int, *, cap: int = DEFAULT_COOCCURRENCE_CAP
) -> tuple[int, int, int]:
    """Genera (determinista, idempotente) las pistas de co-ocurrencia del user: materializa un par
    por cada dos vértices de un mismo mensaje (acotado por `cap`), suprime los pares ya vouchados y
    confirma por regla las pistas redundantes pre-existentes. Es la primera parte de la fase de
    co-ocurrencia (la juzga después `run_per_message_confirm`). NO dispara extracción/consolidación:
    consume los vértices ya proyectados. Devuelve (pistas, saltados, redundantes)."""
    confirmed_pairs = {
        frozenset((e.src, e.dst)) for e in list_edges(conn, user_id, verdict=VERDICT_CONFIRMED)
    }
    prov = vertex_inbox_ids(conn, user_id)
    pistas, skipped = _materialize_cooccurrence(conn, user_id, prov, cap, confirmed_pairs)
    redundant = _resolve_redundant_cooccurrence(conn, user_id, confirmed_pairs)
    _log.info(
        "relation.cooccurrence.generated",
        user_id=user_id,
        pistas=pistas,
        high_fanout_skipped=skipped,
        redundant_resolved=redundant,
    )
    return pistas, skipped, redundant


def dense_message_vertices(
    conn: Connection, user_id: int, *, cap: int = DEFAULT_COOCCURRENCE_CAP
) -> dict[int, list[Ref]]:
    """Por cada mensaje DENSO (más de `_eff_cap` vértices de CONTENIDO — el que
    `_materialize_cooccurrence` SALTEA por fan-out, perdiendo sus pares), sus vértices de contenido
    (CANAL excluido), ordenados canónicamente. Es la entrada de la fase de PROPUESTA del juez
    relacional (`relations.per_message`): para estos mensajes NO hay pistas dibujadas que juzgar,
    así que el LLM PROPONE los pares all-type desde esta lista. Mismo predicado de densidad que el
    salto (vía `_eff_cap`), para no divergir. Solo lectura (no escribe ni dispara nada)."""
    prov = vertex_inbox_ids(conn, user_id)
    by_msg: dict[int, set[Ref]] = defaultdict(set)
    for ref, ids in prov.items():
        for mid in ids:
            by_msg[mid].add(ref)
    chat_ids = _chat_message_ids(conn, user_id, by_msg.keys())
    out: dict[int, list[Ref]] = {}
    for mid, refs in by_msg.items():
        content = sorted((r for r in refs if r.slug != CANAL_SLUG), key=lambda r: (r.slug, r.id))
        if len(content) > _eff_cap(mid, chat_ids, cap):
            out[mid] = content
    return out
