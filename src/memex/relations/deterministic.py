"""Paso de relaciones DETERMINISTAS del grafo: materializa, sin LLM, las aristas derivables de los
datos ya guardados. Es un paso más del pipeline (on-demand, apagado por default, encadenable por el
daemon): CONSUME lo disponible, no dispara pasos previos. Idempotente.

Produce las clases de arista (ver `memex.relations.edges`):
- PISTAS de co-ocurrencia (`producer='inbox'`, `status='pista'`): dos vértices del MISMO mensaje.
  Señal barata de "quizás se relacionan" — NO asegura relación (el LLM la valida después). Se acota
  el fan-out: un mensaje con demasiados vértices (digest) se SALTA y se loguea. Un par que YA tiene
  arista confirmada (cualquier producer/relation_type/orientación) se suprime — y la pista
  redundante pre-existente se poda — porque no aporta sobre la relación vouchada y, promovida,
  doble-contaría el peso del par en la clusterización.
- REALES de afiliación/pertenencia (`producer='identidades'`, `status='confirmed'`): persona↔org y
  sub→padre que el directorio enlaza explícitamente (dato, no adivinanza).
- REALES de contraparte (`producer='finance'`, `status='confirmed'`): cobro/pago CONSOLIDADO →
  identidad del cobrador/pagador (su `counterparty_identity_id` resuelto). El enlace por identidad
  entre finanzas y el directorio — determinista, el conector más valioso del grafo.
- REALES de cumplimiento (`producer='bienestar'`, `status='confirmed'`): registro de bienestar →
  hábito que cumple, por match determinista de `activity` (normalizada) o `category` — la misma
  lógica que la adherencia (`habits._period_counts`).

La provenance vértice→mensaje NO es arista (inbox es atributo): vive en `source_inbox_ids`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.relations.edges import (
    PRODUCER_BIENESTAR,
    PRODUCER_EVENT,
    PRODUCER_FINANCE,
    PRODUCER_IDENTIDADES,
    PRODUCER_INBOX,
    RELTYPE_COOCURRENCIA,
    STATUS_CONFIRMED,
    STATUS_PISTA,
    Ref,
    list_edges,
    propose_edge,
)
from memex.relations.vertices import list_vertices

_log = get_logger("memex.relations.deterministic")

#: Tope de vértices por mensaje para emitir co-ocurrencia. Un mensaje con más (digest/newsletter) se
#: salta: ahí la co-ocurrencia es ruido (C(n,2) aristas sin sentido). Los saltados se loguean.
DEFAULT_COOCCURRENCE_CAP = 8

#: Vértices cuyo enlace a inbox es DIRECTO (columna `source_inbox_ids`): slug → tabla. finance ya NO
#: está acá: su vértice es el CONSOLIDADO, cuya procedencia es TRANSITIVA (vía links → crudos).
_DIRECT_SOURCES: tuple[tuple[str, str], ...] = (("hackathones", "mod_hackathones_events"),)

#: Normalización SQL del `activity` para el match registro↔hábito (lower + colapso de whitespace).
#: Copia DELIBERADA del fragmento de `bienestar` (habits.py / module.py): NO se importa de ahí
#: porque esos módulos ya importan este paquete → invertir la dependencia crearía un ciclo.
_NORM_ACTIVITY = "lower(btrim(regexp_replace({x}, '\\s+', ' ', 'g')))"


@dataclass(frozen=True)
class RelationStats:
    """Resumen de un paso determinista."""

    cooccurrence_pistas: int
    afiliacion_reales: int
    high_fanout_skipped: int
    pertenencia_reales: int = 0
    contraparte_reales: int = 0
    same_event_reales: int = 0
    cumple_reales: int = 0
    orphans_pruned: int = 0
    redundant_pruned: int = 0  # pistas cooc podadas: ya hay confirmada entre los mismos vértices
    cluster_edges: int = 0  # aristas miembro_de vivas tras materializar los cúmulos confirmados


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
    conn: Connection,
    user_id: int,
    prov: dict[Ref, set[int]],
    cap: int,
    confirmed_pairs: set[frozenset[Ref]],
) -> tuple[int, int]:
    """Por cada mensaje, una arista-pista entre cada par de vértices que salieron de él (par
    canónico ordenado). Salta mensajes con más de `cap` vértices y los pares de `confirmed_pairs`
    (ya hay arista confirmada entre ambos, cualquier orientación): la pista no aporta sobre una
    relación vouchada y, promovida por la cascada, doble-contaría el peso del par en la
    clusterización. Devuelve (pistas, saltados)."""
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
                if frozenset(pair) in confirmed_pairs:
                    continue
                propose_edge(
                    conn,
                    user_id,
                    uniq[i],
                    uniq[j],
                    producer=PRODUCER_INBOX,
                    relation_type=RELTYPE_COOCURRENCIA,
                    evidence=f"inbox:{mid}",
                )
                pistas += 1
    return pistas, skipped


def _materialize_afiliacion(
    conn: Connection, user_id: int, *, person_ids: Sequence[int] | None = None
) -> int:
    """Una arista REAL persona→org por cada enlace explícito del directorio. Con `person_ids` acota
    a esas personas (uso incremental); sin él, barre todas (full-sweep). Idempotente. Devuelve
    cuántas."""
    scope = "" if person_ids is None else " AND person_id = ANY(:pids)"
    params: dict[str, Any] = {"u": user_id}
    if person_ids is not None:
        params["pids"] = list(person_ids)
    n = 0
    for r in conn.execute(
        text(
            f"SELECT person_id, org_id FROM mod_identidades_person_orgs WHERE user_id = :u{scope}"
        ),
        params,
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


def _materialize_contraparte(
    conn: Connection, user_id: int, *, consolidated_ids: Sequence[int] | None = None
) -> int:
    """Una arista REAL «contraparte» cobro→identidad por cada transacción CONSOLIDADA cuya
    contraparte resolvió a una identidad del directorio (`counterparty_identity_id`). Dirigida (el
    cobro/pago → quién cobró/pagó). El enlace por identidad finanzas↔directorio. Con
    `consolidated_ids` acota a esos consolidados (incremental); sin él, barre todos. Idempotente."""
    scope = "" if consolidated_ids is None else " AND c.id = ANY(:cids)"
    params: dict[str, Any] = {"u": user_id}
    if consolidated_ids is not None:
        params["cids"] = list(consolidated_ids)
    n = 0
    for r in conn.execute(
        text(
            f"""
            SELECT c.id AS cid, i.id AS iid, i.kind AS kind
            FROM mod_finance_consolidated c
            JOIN mod_identidades i ON i.id = c.counterparty_identity_id
            WHERE c.user_id = :u AND NOT c.deleted AND c.counterparty_identity_id IS NOT NULL{scope}
            """
        ),
        params,
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


def _materialize_same_event(
    conn: Connection, user_id: int, *, event_ids: Sequence[str] | None = None
) -> int:
    """Una arista REAL «mismo_evento» entre hechos que comparten `event_id` — los que el agente
    (Hermes) correlacionó en un mismo mensaje. CROSS-MODULE: bienestar (el registro) y finanzas (la
    transacción mapeada a su CONSOLIDADO, el vértice del grafo); `event_id` NULL no correlaciona.
    Par canónico por `(slug, id)`. Con `event_ids` acota a esos eventos (uso incremental, ambos
    brazos del CTE); sin él, barre todos (full-sweep). Idempotente. Devuelve cuántas."""
    scope_b = "" if event_ids is None else " AND event_id = ANY(:eids)"
    scope_f = "" if event_ids is None else " AND t.event_id = ANY(:eids)"
    params: dict[str, Any] = {"u": user_id}
    if event_ids is not None:
        params["eids"] = list(event_ids)
    n = 0
    for r in conn.execute(
        text(
            f"""
            WITH facts AS (
                SELECT 'bienestar' AS slug, id AS vid, event_id
                FROM mod_bienestar_registros
                WHERE user_id = :u AND event_id IS NOT NULL{scope_b}
                UNION
                SELECT 'finance' AS slug, c.id AS vid, t.event_id
                FROM mod_finance_transactions t
                JOIN mod_finance_transaction_links l ON l.transaction_id = t.id
                JOIN mod_finance_consolidated c ON c.id = l.consolidated_id AND NOT c.deleted
                WHERE t.user_id = :u AND t.event_id IS NOT NULL{scope_f}
            )
            SELECT a.slug AS a_slug, a.vid AS a_vid, b.slug AS b_slug, b.vid AS b_vid
            FROM facts a
            JOIN facts b ON a.event_id = b.event_id AND (a.slug, a.vid) < (b.slug, b.vid)
            """
        ),
        params,
    ).mappings():
        propose_edge(
            conn,
            user_id,
            Ref(str(r["a_slug"]), int(r["a_vid"])),
            Ref(str(r["b_slug"]), int(r["b_vid"])),
            producer=PRODUCER_EVENT,
            relation_type="mismo_evento",
            status=STATUS_CONFIRMED,
            evidence="event_id",
        )
        n += 1
    return n


def _materialize_cumple(
    conn: Connection,
    user_id: int,
    *,
    registro_ids: Sequence[int] | None = None,
    habit_ids: Sequence[int] | None = None,
) -> int:
    """Una arista REAL «cumple» registro→hábito por cada registro de bienestar que satisface un
    hábito ACTIVO. Match determinista idéntico a la adherencia (`habits._period_counts`): si el
    hábito define `activity`, iguala la actividad normalizada (insensible a mayúsculas/espacios);
    si no, iguala la `category`. Dirigida (registro → hábito). Un registro puede cumplir varios
    hábitos. Con `registro_ids`/`habit_ids` acota a esos lados (uso incremental); sin ellos barre
    todo (full-sweep). Idempotente. Devuelve cuántas."""
    scope = ""
    params: dict[str, Any] = {"u": user_id}
    if registro_ids is not None:
        scope += " AND r.id = ANY(:rids)"
        params["rids"] = list(registro_ids)
    if habit_ids is not None:
        scope += " AND h.id = ANY(:hids)"
        params["hids"] = list(habit_ids)
    norm_r = _NORM_ACTIVITY.format(x="r.activity")
    norm_h = _NORM_ACTIVITY.format(x="h.activity")
    n = 0
    for r in conn.execute(
        text(
            f"""
            SELECT r.id AS rid, h.id AS hid
            FROM mod_bienestar_registros r
            JOIN mod_bienestar_habits h ON h.user_id = r.user_id AND h.active AND (
                (h.activity <> '' AND {norm_r} = {norm_h})
                OR (h.activity = '' AND r.category = h.category)
            )
            WHERE r.user_id = :u{scope}
            """
        ),
        params,
    ).mappings():
        propose_edge(
            conn,
            user_id,
            Ref("bienestar", int(r["rid"])),
            Ref("bienestar:habito", int(r["hid"])),
            producer=PRODUCER_BIENESTAR,
            relation_type="cumple",
            status=STATUS_CONFIRMED,
            evidence="cumple",
        )
        n += 1
    return n


def _prune_redundant_cooccurrence(
    conn: Connection, user_id: int, confirmed_pairs: set[frozenset[Ref]]
) -> int:
    """Borra las pistas de co-ocurrencia que quedaron REDUNDANTES: ya existe una arista confirmada
    entre los mismos dos vértices (cualquier producer/relation_type/orientación). Mismo
    triple-filtro (pista + inbox + co-ocurrencia) que la cascada del partidor: las ya promovidas a
    `confirmed` NO se tocan. Devuelve cuántas borró."""
    redundant = [
        e.id
        for e in list_edges(conn, user_id, status=STATUS_PISTA, producer=PRODUCER_INBOX)
        if e.relation_type == RELTYPE_COOCURRENCIA and frozenset((e.src, e.dst)) in confirmed_pairs
    ]
    if redundant:
        conn.execute(
            text("DELETE FROM relation_edges WHERE user_id = :u AND id = ANY(:ids)"),
            {"u": user_id, "ids": redundant},
        )
    return len(redundant)


def prune_orphan_edges(conn: Connection, user_id: int) -> int:
    """Borra de `relation_edges` toda arista con un extremo que ya no resuelve a un vértice vivo
    (consolidado tombstoneado, fila borrada, identidad absorbida en un merge…). Usa la MISMA
    proyección que LEE el grafo (`list_vertices`): prune y lectura nunca divergen. Paso FINAL de
    `build_relations` (tras los materializadores aditivos). Devuelve cuántas borró. NOTA: un
    `(slug, id)` que hoy no proyecte `list_vertices` se trata como huérfano; los cúmulos (vértices
    nativos `cumulo`) YA están en `NODE_SOURCES` (solo confirmados): sus `miembro_de` sobreviven
    y las de un cúmulo disuelto (que deja de proyectar) se barren acá."""
    live = {v.ref for v in list_vertices(conn, user_id)}
    orphan_ids = [e.id for e in list_edges(conn, user_id) if e.src not in live or e.dst not in live]
    if orphan_ids:
        conn.execute(
            text("DELETE FROM relation_edges WHERE user_id = :u AND id = ANY(:ids)"),
            {"u": user_id, "ids": orphan_ids},
        )
    return len(orphan_ids)


def build_relations(
    conn: Connection, user_id: int, *, cooccurrence_cap: int = DEFAULT_COOCCURRENCE_CAP
) -> RelationStats:
    """Materializa las aristas deterministas del user (idempotente). Consume lo disponible; NO
    dispara extracción/consolidación. Devuelve el resumen."""
    # REALES primero: la co-ocurrencia consulta las confirmadas (las de ESTA corrida incluidas)
    # para suprimir pistas redundantes sobre pares ya vouchados.
    afil = _materialize_afiliacion(conn, user_id)
    pert = _materialize_pertenencia(conn, user_id)
    contraparte = _materialize_contraparte(conn, user_id)
    same_event = _materialize_same_event(conn, user_id)
    cumple = _materialize_cumple(conn, user_id)
    confirmed_pairs = {
        frozenset((e.src, e.dst)) for e in list_edges(conn, user_id, status=STATUS_CONFIRMED)
    }
    prov = vertex_inbox_ids(conn, user_id)
    pistas, skipped = _materialize_cooccurrence(
        conn, user_id, prov, cooccurrence_cap, confirmed_pairs
    )
    redundant = _prune_redundant_cooccurrence(conn, user_id, confirmed_pairs)
    # Re-deriva las aristas `miembro_de` de los cúmulos confirmados (idempotente + GC de podados o
    # movidos) ANTES del prune. Import local: no acopla networkx al import de este módulo.
    from memex.relations.cluster_store import materialize_cluster_edges

    cluster_edges = materialize_cluster_edges(conn, user_id)
    # Paso FINAL: barrer aristas huérfanas (extremo ido: tombstone/borrado/merge/cúmulo disuelto).
    # Idempotente: los materializadores son aditivos y nunca re-crean aristas a vértices muertos.
    pruned = prune_orphan_edges(conn, user_id)
    stats = RelationStats(
        cooccurrence_pistas=pistas,
        afiliacion_reales=afil,
        high_fanout_skipped=skipped,
        pertenencia_reales=pert,
        contraparte_reales=contraparte,
        same_event_reales=same_event,
        cumple_reales=cumple,
        orphans_pruned=pruned,
        redundant_pruned=redundant,
        cluster_edges=cluster_edges,
    )
    _log.info(
        "relation.build.done",
        user_id=user_id,
        cooccurrence_pistas=pistas,
        afiliacion_reales=afil,
        pertenencia_reales=pert,
        contraparte_reales=contraparte,
        same_event_reales=same_event,
        cumple_reales=cumple,
        high_fanout_skipped=skipped,
        orphans_pruned=pruned,
        redundant_pruned=redundant,
        cluster_edges=cluster_edges,
    )
    return stats


def weave_event(conn: Connection, user_id: int, event_id: str) -> int:
    """INCREMENTAL: teje las aristas «mismo_evento» de UN evento, en la misma tx del caller. Lo
    llaman los módulos al escribir un hecho con `event_id`, para no depender del full-sweep. Si el
    otro extremo aún no existe, no crea nada todavía (lo creará quien aterrice último). Idempotente.
    Devuelve cuántas aristas tocó."""
    if not event_id:
        return 0
    return _materialize_same_event(conn, user_id, event_ids=[event_id])


def weave_afiliacion(conn: Connection, user_id: int, person_id: int) -> int:
    """INCREMENTAL: teje la arista «afiliado» de las afiliaciones de UNA persona recién enlazada, en
    la misma tx del caller. Lo llama `identidades.register_card` al crear la afiliación, para no
    depender del full-sweep. Ambos extremos (persona/org) ya existen. Idempotente. Devuelve
    cuántas."""
    return _materialize_afiliacion(conn, user_id, person_ids=[person_id])


def weave_finance_consolidated(
    conn: Connection,
    user_id: int,
    consolidated_ids: Sequence[int],
    event_ids: Sequence[str],
) -> tuple[int, int]:
    """INCREMENTAL: tras consolidar finanzas (donde nace su vértice), teje «contraparte» de esos
    consolidados y «mismo_evento» de sus eventos, en la misma tx. Idempotente. Devuelve
    (contraparte, mismo_evento)."""
    cids = list(consolidated_ids)
    eids = [e for e in event_ids if e]
    contraparte = _materialize_contraparte(conn, user_id, consolidated_ids=cids) if cids else 0
    same_event = _materialize_same_event(conn, user_id, event_ids=eids) if eids else 0
    return contraparte, same_event


def weave_cumple(
    conn: Connection,
    user_id: int,
    *,
    registro_ids: Sequence[int] | None = None,
    habit_ids: Sequence[int] | None = None,
) -> int:
    """INCREMENTAL: teje las aristas «cumple» de UN registro recién creado (`registro_ids`) o de UN
    hábito recién creado (`habit_ids`), en la misma tx del caller, para no depender del full-sweep.
    Lo llaman `bienestar.register` (lado registro) y `bienestar.add_habit` (lado hábito). Si el otro
    lado aún no existe, no crea nada todavía (lo creará quien aterrice último). Idempotente.
    Devuelve cuántas."""
    return _materialize_cumple(conn, user_id, registro_ids=registro_ids, habit_ids=habit_ids)
