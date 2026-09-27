"""Libro de evidencia de vínculos (`identidades/links.py`): reclamos idempotentes, padre efectivo
derivado por soporte (no «último correo gana»), pin manual/agent/curator, anti-ciclo, rol efectivo
más sustentado, resolución de nombres sin inventar orgs, re-apunte al fusionar y siembra.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.links import (
    CLAIM_AFILIADO,
    CLAIM_PERTENECE,
    METHOD_AGENT,
    METHOD_LLM,
    METHOD_ORGANIZER,
    HierarchySettings,
    ParentCandidate,
    affiliate,
    choose_parent,
    claim_and_derive_parent,
    derive_parent,
    effective_role,
    hierarchy_nodes,
    list_claims,
    merge_blocked_reason,
    parent_candidates,
    record_claim,
    resolve_target_name,
    seed_claims,
    set_claim_status,
)
from memex.modules.identidades.merge import merge_identities


def _mk(conn: Any, kind: str, name: str, aliases: list[str] | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, aliases) "
                "VALUES (1, :k, :n, :a) RETURNING id"
            ),
            {"k": kind, "n": name, "a": aliases or []},
        ).scalar_one()
    )


def _parent(conn: Any, ident: int) -> tuple[int | None, str | None]:
    row = conn.execute(
        text(
            "SELECT parent_identity_id, metadata->>'parent_source' FROM mod_identidades "
            "WHERE id = :id"
        ),
        {"id": ident},
    ).one()
    return (int(row[0]) if row[0] is not None else None, row[1])


def _cand(
    tid: int, msgs: int, support: float, best: float, pinned: bool = False
) -> ParentCandidate:
    return ParentCandidate(tid, f"org{tid}", msgs, support, best, ("llm",), pinned)


# ----- regla pura ------------------------------------------------------------------ #


def test_choose_parent_rules() -> None:
    st = HierarchySettings(parent_min_messages=2, parent_single_min_confidence=0.9)
    # un solo correo a 0.85 no califica → se queda el actual (None)
    assert choose_parent([_cand(10, 1, 0.85, 0.85)], current_parent=None, settings=st) is None
    # un solo correo con confianza alta sí
    assert choose_parent([_cand(10, 1, 0.95, 0.95)], current_parent=None, settings=st) == 10
    # dos correos a 0.85 ganan a uno a 0.95 (soporte 1.7 > 0.95)
    assert (
        choose_parent(
            [_cand(10, 1, 0.95, 0.95), _cand(20, 2, 1.7, 0.85)], current_parent=None, settings=st
        )
        == 20
    )
    # un correo desviado NO tumba un padre vigente con más soporte
    assert (
        choose_parent(
            [_cand(20, 3, 2.5, 0.9), _cand(10, 1, 0.95, 0.95)], current_parent=20, settings=st
        )
        == 20
    )
    # empate de soporte → se queda el actual
    assert (
        choose_parent(
            [_cand(10, 2, 1.7, 0.85), _cand(20, 2, 1.7, 0.85)], current_parent=20, settings=st
        )
        == 20
    )
    # pin manda aunque tenga menos soporte
    assert (
        choose_parent(
            [_cand(20, 3, 2.5, 0.9), _cand(10, 1, 1.0, 1.0, pinned=True)],
            current_parent=20,
            settings=st,
        )
        == 10
    )


# ----- libro + derivación (DB) --------------------------------------------------------- #


def test_record_claim_idempotente_por_correo_y_metodo(conn: Any) -> None:
    child = _mk(conn, "organizacion", "Carrera de Sistemas")
    uni = _mk(conn, "organizacion", "Universidad X")
    a = record_claim(
        conn,
        1,
        kind=CLAIM_PERTENECE,
        source_id=child,
        target_id=uni,
        confidence=0.8,
        method=METHOD_LLM,
        inbox_id=100,
    )
    b = record_claim(
        conn,
        1,
        kind=CLAIM_PERTENECE,
        source_id=child,
        target_id=uni,
        confidence=0.9,
        method=METHOD_LLM,
        inbox_id=100,
    )
    assert a == b  # mismo correo+método → mismo reclamo, confianza = la mayor
    claims = list_claims(conn, 1, source_id=child)
    assert len(claims) == 1 and claims[0].confidence == 0.9
    # self-link y sin destino → inválidos
    assert (
        record_claim(
            conn, 1, kind=CLAIM_PERTENECE, source_id=child, target_id=child, method=METHOD_LLM
        )
        is None
    )
    assert record_claim(conn, 1, kind=CLAIM_PERTENECE, source_id=child, method=METHOD_LLM) is None


def test_derive_parent_no_es_ultimo_correo_gana(conn: Any) -> None:
    child = _mk(conn, "organizacion", "Semillero Z")
    uni = _mk(conn, "organizacion", "Universidad X")
    otra = _mk(conn, "organizacion", "Empresa Y")
    # Dos correos dicen «Universidad X» (0.85 cada uno)
    for inbox in (1, 2):
        res = claim_and_derive_parent(
            conn,
            1,
            child_id=child,
            target_id=uni,
            target_name="",
            confidence=0.85,
            method=METHOD_LLM,
            inbox_id=inbox,
        )
    assert res is not None and res.parent_id == uni
    assert _parent(conn, child) == (uni, "derived")
    # Un tercer correo, desviado, dice «Empresa Y» con 0.95: NO tumba (0.95 < 1.7)
    res = claim_and_derive_parent(
        conn,
        1,
        child_id=child,
        target_id=otra,
        target_name="",
        confidence=0.95,
        method=METHOD_LLM,
        inbox_id=3,
    )
    assert res is not None and not res.changed and res.parent_id == uni
    # Pero si otros dos correos también dicen «Empresa Y», el soporte cambia de lado
    for inbox in (4, 5):
        res = claim_and_derive_parent(
            conn,
            1,
            child_id=child,
            target_id=otra,
            target_name="",
            confidence=0.9,
            method=METHOD_LLM,
            inbox_id=inbox,
        )
    assert _parent(conn, child) == (otra, "derived")
    cands = parent_candidates(conn, 1, child)
    assert [(c.target_id, c.messages) for c in cands] == [(otra, 3), (uni, 2)]


def test_derive_respeta_pin_y_curator_pinea(conn: Any) -> None:
    child = _mk(conn, "organizacion", "Programa P")
    uni = _mk(conn, "organizacion", "Universidad X")
    otra = _mk(conn, "organizacion", "Empresa Y")
    record_claim(conn, 1, kind=CLAIM_PERTENECE, source_id=child, target_id=uni, method=METHOD_AGENT)
    conn.execute(
        text(
            "UPDATE mod_identidades SET parent_identity_id = :p, "
            "metadata = jsonb_set(metadata, '{parent_source}', '\"agent\"') WHERE id = :c"
        ),
        {"p": uni, "c": child},
    )
    for inbox in (1, 2, 3):
        claim_and_derive_parent(
            conn,
            1,
            child_id=child,
            target_id=otra,
            target_name="",
            confidence=0.95,
            method=METHOD_LLM,
            inbox_id=inbox,
        )
    assert _parent(conn, child) == (uni, "agent")  # pineado: tres correos no lo mueven
    assert derive_parent(conn, 1, child).reason == "pinned"


def test_derive_salta_ciclo_y_elige_el_siguiente(conn: Any) -> None:
    a = _mk(conn, "organizacion", "A")
    b = _mk(conn, "organizacion", "B")
    c = _mk(conn, "organizacion", "C")
    claim_and_derive_parent(
        conn,
        1,
        child_id=a,
        target_id=b,
        target_name="",
        confidence=0.95,
        method=METHOD_LLM,
        inbox_id=1,
    )
    assert _parent(conn, a)[0] == b
    # B → A crearía ciclo (aunque tenga más soporte); B → C es el siguiente válido
    for inbox in (2, 3, 4):
        record_claim(
            conn,
            1,
            kind=CLAIM_PERTENECE,
            source_id=b,
            target_id=a,
            confidence=0.9,
            method=METHOD_LLM,
            inbox_id=inbox,
        )
    record_claim(
        conn,
        1,
        kind=CLAIM_PERTENECE,
        source_id=b,
        target_id=c,
        confidence=0.95,
        method=METHOD_LLM,
        inbox_id=5,
    )
    res = derive_parent(conn, 1, b)
    assert res.changed and res.parent_id == c


def test_reclamo_rechazado_no_cuenta(conn: Any) -> None:
    child = _mk(conn, "organizacion", "Área Q")
    uni = _mk(conn, "organizacion", "Universidad X")
    cid = claim_and_derive_parent(
        conn,
        1,
        child_id=child,
        target_id=uni,
        target_name="",
        confidence=0.95,
        method=METHOD_LLM,
        inbox_id=1,
    )
    assert cid is not None and _parent(conn, child)[0] == uni
    claim = list_claims(conn, 1, source_id=child)[0]
    assert set_claim_status(conn, 1, claim.id, "rejected") is not None
    assert parent_candidates(conn, 1, child) == []
    # sin candidatos la derivación conserva el actual (estabilidad; quitarlo es decisión del dueño)
    assert derive_parent(conn, 1, child).reason == "no_candidate"


# ----- nombres sin inventar orgs -------------------------------------------------------- #


def test_resolve_target_name_cascada_sin_crear(conn: Any) -> None:
    uni = _mk(conn, "organizacion", "Pontificia Universidad Salesiana", aliases=["PUJ"])
    assert resolve_target_name(conn, 1, "Pontificia Universidad Salesiana") == uni
    assert resolve_target_name(conn, 1, "PUJ") == uni  # alias
    assert resolve_target_name(conn, 1, "Universidad que no existe") is None
    assert resolve_target_name(conn, 1, "noreply@x.com") is None
    total = conn.execute(
        text("SELECT count(*) FROM mod_identidades WHERE user_id = 1")
    ).scalar_one()
    assert total == 1  # nada nuevo


def test_claim_por_nombre_sin_resolver_queda_en_el_libro(conn: Any) -> None:
    child = _mk(conn, "organizacion", "Impulsa Bootcamp")
    res = claim_and_derive_parent(
        conn,
        1,
        child_id=child,
        target_id=None,
        target_name="Blockchain Acceleration Foundation",
        confidence=0.9,
        method=METHOD_ORGANIZER,
    )
    assert res is not None and res.reason == "unresolved_name"
    assert _parent(conn, child) == (None, None)
    nodes = {n.id: n for n in hierarchy_nodes(conn, 1)}
    assert nodes[child].unresolved_names == ("Blockchain Acceleration Foundation",)


# ----- afiliación: rol efectivo ------------------------------------------------------- #


def test_affiliate_rol_efectivo_mas_sustentado(conn: Any) -> None:
    p = _mk(conn, "persona", "Luisa Garzón")
    o = _mk(conn, "organizacion", "Carrera de Electrónica")
    affiliate(conn, 1, p, o, "asistente de carrera", method=METHOD_LLM, inbox_id=1, confidence=0.9)
    affiliate(conn, 1, p, o, "Profesional I - Asistente de Carrera", method=METHOD_LLM, inbox_id=2)
    affiliate(conn, 1, p, o, "asistente de carrera", method=METHOD_LLM, inbox_id=3)
    assert effective_role(conn, 1, p, o) == "asistente de carrera"
    role = conn.execute(
        text("SELECT role FROM mod_identidades_person_orgs WHERE person_id = :p AND org_id = :o"),
        {"p": p, "o": o},
    ).scalar_one()
    assert role == "asistente de carrera"  # no «el último», el más sustentado
    edges = conn.execute(
        text("SELECT count(*) FROM relation_edges WHERE user_id = 1 AND relation_type = 'afiliado'")
    ).scalar_one()
    assert edges == 1
    assert len(list_claims(conn, 1, source_id=p, kind=CLAIM_AFILIADO)) == 3


# ----- fusión: el libro se re-apunta --------------------------------------------------- #


def test_merge_reapunta_reclamos_y_suma_soporte(conn: Any) -> None:
    child = _mk(conn, "organizacion", "FIB's Newsletter")
    fib = _mk(conn, "organizacion", "FIB")
    largo = _mk(conn, "organizacion", "Blockchain Acceleration Foundation")
    claim_and_derive_parent(
        conn,
        1,
        child_id=child,
        target_id=fib,
        target_name="",
        confidence=0.85,
        method=METHOD_LLM,
        inbox_id=1,
    )
    claim_and_derive_parent(
        conn,
        1,
        child_id=child,
        target_id=largo,
        target_name="",
        confidence=0.85,
        method=METHOD_LLM,
        inbox_id=2,
    )
    assert merge_identities(conn, 1, fib, largo)
    cands = parent_candidates(conn, 1, child)
    assert [(c.target_id, c.messages) for c in cands] == [(fib, 2)]
    assert derive_parent(conn, 1, child).parent_id == fib


# ----- siembra ----------------------------------------------------------------------- #


def test_seed_claims_desde_estado_previo(conn: Any) -> None:
    child = _mk(conn, "organizacion", "Carrera Q")
    uni = _mk(conn, "organizacion", "Universidad X")
    p = _mk(conn, "persona", "Ana")
    conn.execute(
        text(
            "UPDATE mod_identidades SET parent_identity_id = :p, "
            "metadata = jsonb_set(metadata, '{parent_source}', '\"llm\"') WHERE id = :c"
        ),
        {"p": uni, "c": child},
    )
    conn.execute(
        text(
            "INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id, role, source) "
            "VALUES (1, :p, :o, 'profesora', 'extraction')"
        ),
        {"p": p, "o": uni},
    )
    conn.execute(
        text(
            "INSERT INTO mod_identidades_mentions (user_id, source_inbox_ids, evidence, "
            "mentioned_name, mentioned_kind, org_hint, role_hint, confidence, resolved_kind, "
            "resolved_identity_id, resolution_method) VALUES (1, ARRAY[7]::bigint[], 'x', 'Ana', "
            "'persona', 'Universidad X', 'docente', 0.9, 'persona', :p, 'exact_name')"
        ),
        {"p": p},
    )
    st = seed_claims(conn, 1)
    assert (st.parents, st.affiliations, st.mention_affiliations) == (1, 1, 1)
    assert [c.method for c in list_claims(conn, 1, source_id=child)] == ["legacy"]
    af = list_claims(conn, 1, source_id=p, kind=CLAIM_AFILIADO)
    assert {(c.method, c.inbox_id, c.role) for c in af} == {
        ("legacy", None, "profesora"),
        ("llm", 7, "docente"),
    }
    # idempotente
    st2 = seed_claims(conn, 1)
    assert (st2.parents, st2.affiliations, st2.mention_affiliations) == (1, 1, 1)
    assert len(list_claims(conn, 1, source_id=p, kind=CLAIM_AFILIADO)) == 2


def test_merge_blocked_reason(conn: Any) -> None:
    uni = _mk(conn, "organizacion", "Universidad X")
    fac = _mk(conn, "organizacion", "Facultad Y")
    dep = _mk(conn, "organizacion", "Departamento Z")
    dup = _mk(conn, "organizacion", "Universidad X (razón social)")
    conn.execute(
        text("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :c"),
        {"p": uni, "c": fac},
    )
    conn.execute(
        text("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :c"),
        {"p": fac, "c": dep},
    )
    assert merge_blocked_reason(conn, 1, uni, dep) == "hierarchy"  # nieta ↔ abuela
    assert merge_blocked_reason(conn, 1, dep, uni) == "hierarchy"
    assert merge_blocked_reason(conn, 1, uni, dup) is None  # duplicado legítimo: puede
    conn.execute(
        text(
            "UPDATE mod_identidades SET metadata = jsonb_set(metadata, '{parent_source}', "
            "'\"manual\"') WHERE id = :c"
        ),
        {"c": dup},
    )
    assert merge_blocked_reason(conn, 1, uni, dup) == "pinned"
    assert merge_blocked_reason(conn, 1, uni, 999_999) is None  # inexistente: lo resuelve merge
