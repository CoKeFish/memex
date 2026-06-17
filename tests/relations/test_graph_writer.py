"""GraphWriter (chokepoint del grafo): toda mutación deja el delta `dirty` COMPLETO.

Foco de este slice: las aristas (`add_edge`/`update_verdict`/`delete_edge`/`prune_edges`) y el
marcado de vértices (`add_vertex`/`update_vertex`) marcan dirty a quien corresponde, y la
propagación a vecinos respeta `hops`. El hueco que cierra el sistema: un RECHAZO marca AMBOS
extremos (antes solo tocaba la arista).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.relations.edges import (
    PRODUCER_INBOX,
    PROVENANCE_EXTRACTED,
    PROVENANCE_INFERRED,
    VERDICT_CONFIRMED,
    VERDICT_REJECTED,
    Ref,
)
from memex.relations.graph_writer import (
    add_edge,
    add_vertex,
    delete_edge,
    delete_vertex,
    mark_dirty,
    merge_vertices,
    prune_edges,
    reject_override,
    update_verdict,
    update_vertex,
)

A = Ref("finance", 1)
B = Ref("calendar", 2)
C = Ref("finance", 3)


def _dirty() -> set[tuple[str, int]]:
    with connection() as c:
        rows = c.execute(
            text("SELECT slug, id FROM relation_vertex_state WHERE user_id = 1 AND dirty")
        ).all()
    return {(str(s), int(i)) for s, i in rows}


def _clear_dirty() -> None:
    with connection() as c:
        c.execute(text("DELETE FROM relation_vertex_state WHERE user_id = 1"))


def _add_edge(src: Ref = A, dst: Ref = B, **kw: Any) -> int:
    with connection() as c:
        return add_edge(c, 1, src, dst, producer=kw.pop("producer", PRODUCER_INBOX), **kw)


def _tag(r: Ref) -> tuple[str, int]:
    return (r.slug, r.id)


def test_add_edge_marca_ambos_extremos() -> None:
    _add_edge(A, B)
    d = _dirty()
    assert _tag(A) in d and _tag(B) in d


def test_update_verdict_confirm_marca_extremos() -> None:
    eid = _add_edge(A, B)
    _clear_dirty()
    with connection() as c:
        changed = update_verdict(
            c, 1, eid, verdict=VERDICT_CONFIRMED, provenance=PROVENANCE_INFERRED
        )
    assert changed is True
    d = _dirty()
    assert _tag(A) in d and _tag(B) in d


def test_update_verdict_reject_marca_ambos_extremos() -> None:
    # EL HUECO QUE CIERRA EL SISTEMA: el rechazo ahora marca dirty los dos extremos (antes no).
    eid = _add_edge(A, B)
    _clear_dirty()
    with connection() as c:
        changed = update_verdict(
            c, 1, eid, verdict=VERDICT_REJECTED, provenance=PROVENANCE_INFERRED
        )
    assert changed is True
    d = _dirty()
    assert _tag(A) in d and _tag(B) in d


def test_delete_edge_marca_extremos_vivos() -> None:
    eid = _add_edge(A, B)
    _clear_dirty()
    with connection() as c:
        assert delete_edge(c, 1, eid) is True
    d = _dirty()
    assert _tag(A) in d and _tag(B) in d
    # la arista se borró
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM relation_edges WHERE user_id = 1")).scalar()
    assert n == 0


def test_prune_edges_marca_todos_los_extremos() -> None:
    e1 = _add_edge(A, B)
    e2 = _add_edge(B, C)
    _clear_dirty()
    with connection() as c:
        n = prune_edges(c, 1, [e1, e2])
    assert n == 2
    d = _dirty()
    assert {_tag(A), _tag(B), _tag(C)} <= d


def test_add_vertex_solo_marca_el_vertice() -> None:
    # aunque exista una arista A-B, add_vertex(A) NO propaga (hops=0): un vértice nuevo no tiene
    # vecinos que reconsiderar.
    _add_edge(A, B)
    _clear_dirty()
    with connection() as c:
        add_vertex(c, 1, A)
    assert _dirty() == {_tag(A)}


def test_update_vertex_marca_vecinos() -> None:
    _add_edge(A, B)
    _clear_dirty()
    with connection() as c:
        update_vertex(c, 1, A)  # hops default = 1 → A + su vecino B
    d = _dirty()
    assert _tag(A) in d and _tag(B) in d


def test_hops_controla_la_propagacion() -> None:
    # cadena A-B-C (dos aristas). mark_dirty([A], hops=N) marca el cierre a N saltos.
    _add_edge(A, B)
    _add_edge(B, C)

    _clear_dirty()
    with connection() as c:
        mark_dirty(c, 1, [A], hops=0)
    assert _dirty() == {_tag(A)}

    _clear_dirty()
    with connection() as c:
        mark_dirty(c, 1, [A], hops=1)
    assert _dirty() == {_tag(A), _tag(B)}

    _clear_dirty()
    with connection() as c:
        mark_dirty(c, 1, [A], hops=2)
    assert _dirty() == {_tag(A), _tag(B), _tag(C)}


# --- fusión / borrado / override (los 3 huecos críticos) -------------------------------- #
P_ABS = Ref("identidades:person", 10)
P_SURV = Ref("identidades:person", 11)
NB = Ref("finance", 5)


def test_merge_vertices_reapunta_aristas_y_marca_vecinos() -> None:
    eid = _add_edge(P_ABS, NB)  # arista del absorbido con un vecino
    _clear_dirty()
    with connection() as c:
        merge_vertices(c, 1, absorbed=P_ABS, survivor=P_SURV)
    # la arista quedó re-apuntada al superviviente (el absorbido ya no aparece)
    with connection() as c:
        e = c.execute(
            text("SELECT src_slug, src_id FROM relation_edges WHERE id = :id"), {"id": eid}
        ).first()
    assert e is not None and (str(e[0]), int(e[1])) == (P_SURV.slug, P_SURV.id)
    # dirty: superviviente + el ex-vecino del absorbido (ANTES nadie los marcaba — el hueco)
    d = _dirty()
    assert _tag(P_SURV) in d and _tag(NB) in d


def test_delete_vertex_borra_aristas_y_marca_vecinos_no_a_si_mismo() -> None:
    _add_edge(P_ABS, NB)
    _clear_dirty()
    with connection() as c:
        delete_vertex(c, 1, P_ABS)
    # las aristas que tocaban el vértice se fueron
    with connection() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM relation_edges WHERE user_id = 1 "
                "AND ((src_slug = :s AND src_id = :i) OR (dst_slug = :s AND dst_id = :i))"
            ),
            {"s": P_ABS.slug, "i": P_ABS.id},
        ).scalar()
    assert n == 0
    d = _dirty()
    assert _tag(NB) in d and _tag(P_ABS) not in d  # el vecino sí, el borrado no


def test_reject_override_rechaza_incluso_un_confirmed() -> None:
    eid = _add_edge(A, B)
    with connection() as c:  # primero confirmar (monótono)
        update_verdict(c, 1, eid, verdict=VERDICT_CONFIRMED, provenance=PROVENANCE_INFERRED)
    _clear_dirty()
    with connection() as c:  # override del humano: baja el confirmed (resolve_edge no podría)
        changed = reject_override(c, 1, eid, evidence="ruido", relation="ruido")
    assert changed is True
    with connection() as c:
        v = c.execute(
            text("SELECT verdict, provenance FROM relation_edges WHERE id = :id"), {"id": eid}
        ).first()
    assert v is not None and v[0] == VERDICT_REJECTED and v[1] == PROVENANCE_EXTRACTED
    d = _dirty()
    assert _tag(A) in d and _tag(B) in d


# --- adversariales: caminos no cubiertos (co-miembros, inexistentes, conflicto UNIQUE) --- #
def _seed_cluster(members: list[Ref]) -> int:
    """Siembra un cúmulo confirmado con esos miembros (para probar la propagación a co-miembros)."""
    with connection() as c:
        cid = c.execute(
            text(
                "INSERT INTO relation_clusters (user_id, status, signature, blob_signature, "
                "member_count) VALUES (1, 'confirmed', :sig, :sig, :mc) RETURNING id"
            ),
            {"sig": f"sig-{members[0].slug}-{members[0].id}", "mc": len(members)},
        ).scalar_one()
        for m in members:
            c.execute(
                text(
                    "INSERT INTO relation_cluster_members "
                    "(user_id, cluster_id, member_slug, member_id) VALUES (1, :c, :s, :i)"
                ),
                {"c": int(cid), "s": m.slug, "i": m.id},
            )
    return int(cid)


def test_merge_vertices_marca_co_miembros_de_cumulo() -> None:
    x = Ref("identidades:person", 30)
    y = Ref("finance", 31)
    _seed_cluster([P_ABS, x, y])
    _clear_dirty()
    with connection() as c:
        merge_vertices(c, 1, absorbed=P_ABS, survivor=P_SURV)
    d = _dirty()
    assert _tag(x) in d and _tag(y) in d  # co-miembros del absorbido se reconsideran


def test_delete_vertex_marca_co_miembros_y_borra_membresia() -> None:
    x = Ref("identidades:person", 32)
    _seed_cluster([P_ABS, x])
    _clear_dirty()
    with connection() as c:
        delete_vertex(c, 1, P_ABS)
    assert _tag(x) in _dirty()
    with connection() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM relation_cluster_members "
                "WHERE user_id = 1 AND member_slug = :s AND member_id = :i"
            ),
            {"s": P_ABS.slug, "i": P_ABS.id},
        ).scalar()
    assert n == 0  # su membresía de cúmulo se borró


def test_update_verdict_arista_inexistente_no_crashea() -> None:
    with connection() as c:
        changed = update_verdict(
            c, 1, 999999, verdict=VERDICT_CONFIRMED, provenance=PROVENANCE_INFERRED
        )
    assert changed is False
    assert _dirty() == set()


def test_delete_edge_inexistente_devuelve_false() -> None:
    with connection() as c:
        assert delete_edge(c, 1, 999999) is False


def test_merge_vertices_noop_si_mismo_o_distinto_slug() -> None:
    with connection() as c:
        merge_vertices(c, 1, absorbed=A, survivor=A)  # mismo vértice
        merge_vertices(c, 1, absorbed=A, survivor=B)  # distinto slug (finance vs calendar)
    assert _dirty() == set()  # ninguno hizo nada


def test_merge_vertices_colapsa_conflicto_unique() -> None:
    # absorbido y superviviente tienen, cada uno, una arista al MISMO destino con igual tipo+
    # productor: re-apuntar crearía un duplicado de la UNIQUE lógica → debe colapsar, no crashear.
    dst = Ref("finance", 40)
    _add_edge(P_ABS, dst)
    _add_edge(P_SURV, dst)
    _clear_dirty()
    with connection() as c:
        merge_vertices(c, 1, absorbed=P_ABS, survivor=P_SURV)
    with connection() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM relation_edges WHERE user_id = 1 "
                "AND src_slug = :s AND src_id = :i AND dst_slug = :ds AND dst_id = :di"
            ),
            {"s": P_SURV.slug, "i": P_SURV.id, "ds": dst.slug, "di": dst.id},
        ).scalar()
    assert n == 1  # se colapsó, sin duplicado ni IntegrityError
