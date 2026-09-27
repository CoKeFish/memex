"""Curador codex de la jerarquía (`identidades/curator.py`): expediente por scope (sin pineados,
sin personas), parseo ultra-defensivo, aplicación de cada acción (pinea `curator`), umbral de
confianza, dry-run y registro en `llm_calls`. codex se inyecta con un `ask` falso."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.curator import (
    CuratorItem,
    CuratorStats,
    apply_decisions,
    build_prompt,
    collect_items,
    parse_decisions,
    run_curator,
)
from memex.modules.identidades.links import (
    CLAIM_PERTENECE,
    METHOD_LLM,
    claim_and_derive_parent,
    list_claims,
    record_claim,
)
from memex.webcontext._codex_subprocess import CodexRun
from memex.webcontext.client import WebContextUsage


def _mk(conn: Any, kind: str, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) VALUES (1, :k, :n) "
                "RETURNING id"
            ),
            {"k": kind, "n": name},
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


def _run(content: str) -> CodexRun:
    return CodexRun(
        last_message=content, usage=WebContextUsage(input_tokens=10, output_tokens=5), latency_ms=7
    )


def test_collect_items_scopes_y_sin_personas(conn: Any) -> None:
    child = _mk(conn, "organizacion", "Área Q")
    uni = _mk(conn, "organizacion", "Universidad X")
    otra = _mk(conn, "organizacion", "Empresa Y")
    junk = _mk(conn, "organizacion", "noreply")
    _mk(conn, "persona", "Ana")
    # disputa: dos padres con soporte parecido
    for inbox in (1, 2):
        claim_and_derive_parent(
            conn,
            1,
            child_id=child,
            target_id=uni,
            target_name="",
            confidence=0.85,
            method=METHOD_LLM,
            inbox_id=inbox,
        )
    for inbox in (3, 4):
        record_claim(
            conn,
            1,
            kind=CLAIM_PERTENECE,
            source_id=child,
            target_id=otra,
            confidence=0.85,
            method=METHOD_LLM,
            inbox_id=inbox,
        )
    # nombre sin resolver
    record_claim(
        conn,
        1,
        kind=CLAIM_PERTENECE,
        source_id=otra,
        target_name="Holding Z",
        method=METHOD_LLM,
        inbox_id=5,
    )
    disputed = collect_items(conn, 1, scope="disputed")
    assert [it.identity_id for it in disputed] == [child]
    assert disputed[0].kind == "parent" and disputed[0].payload["flags"]["disputed"] is True
    assert {c["parent_id"] for c in disputed[0].payload["candidates"]} == {uni, otra}
    assert disputed[0].payload["candidates"][0]["evidence"]  # trae los correos
    unresolved = collect_items(conn, 1, scope="unresolved")
    assert [it.identity_id for it in unresolved] == [otra]
    roots = collect_items(conn, 1, scope="roots")
    assert junk in {it.identity_id for it in roots}
    everything = collect_items(conn, 1, scope="all")
    assert all(it.payload.get("entry", {}).get("kind") != "persona" for it in everything)
    with pytest.raises(ValueError):
        collect_items(conn, 1, scope="personas")


def test_parse_decisions_defensivo() -> None:
    items = [CuratorItem("parent:1", "parent", 1, {}), CuratorItem("merge:9", "merge", 2, {})]
    content = json.dumps(
        {
            "decisions": [
                {
                    "item": "parent:1",
                    "action": "set_parent",
                    "parent_id": 5,
                    "parent_name": None,
                    "survivor_id": None,
                    "absorbed_id": None,
                    "confidence": 0.9,
                    "rationale": "ok",
                },
                {"item": "parent:1", "action": "skip", "confidence": 1},  # repetido → fuera
                {"item": "parent:77", "action": "skip", "confidence": 1},  # fuera del lote
                {"item": "merge:9", "action": "explode", "confidence": 1},  # acción inválida
                {
                    "item": "merge:9",
                    "action": "merge",
                    "survivor_id": "2",
                    "absorbed_id": 3,
                    "confidence": 2.5,
                    "rationale": "x",
                },
            ]
        }
    )
    out = parse_decisions(content, items)
    assert [(d.key, d.action, d.parent_id, d.confidence) for d in out] == [
        ("parent:1", "set_parent", 5, 0.9),
        ("merge:9", "merge", None, 1.0),
    ]
    assert out[1].survivor_id is None and out[1].absorbed_id == 3  # "2" (string) no es id
    assert parse_decisions("no json", items) == []
    assert parse_decisions(json.dumps({"decisions": "x"}), items) == []


def test_apply_set_keep_no_parent_create_delete(conn: Any) -> None:
    child = _mk(conn, "organizacion", "Área Q")
    uni = _mk(conn, "organizacion", "Universidad X")
    otra = _mk(conn, "organizacion", "Empresa Y")
    junk = _mk(conn, "organizacion", "noreply")
    w = _mk(conn, "organizacion", "Empresa W")
    child2 = _mk(conn, "organizacion", "Área R")
    junk2 = _mk(conn, "organizacion", "notify@updates.example")  # basura CON hijo
    child3 = _mk(conn, "organizacion", "Área S")
    for c_, p_ in ((child, otra), (child2, w), (child3, junk2)):
        claim_and_derive_parent(
            conn,
            1,
            child_id=c_,
            target_id=p_,
            target_name="",
            confidence=0.95,
            method=METHOD_LLM,
            inbox_id=c_,
        )
    assert _parent(conn, child)[0] == otra
    items = collect_items(conn, 1, scope="all")
    keys = {it.identity_id: it.key for it in items if it.kind == "parent"}
    # otra y w son raíces sospechosas (hijos sin menciones); child/child2 débiles; junk basura.
    assert {child, otra, junk, w, child2} <= set(keys) and uni not in keys
    content = json.dumps(
        {
            "decisions": [
                {
                    "item": keys[child],
                    "action": "set_parent",
                    "parent_id": uni,
                    "parent_name": None,
                    "survivor_id": None,
                    "absorbed_id": None,
                    "confidence": 0.9,
                    "rationale": "área de la universidad",
                },
                {
                    "item": keys[junk],
                    "action": "delete",
                    "parent_id": None,
                    "parent_name": None,
                    "survivor_id": None,
                    "absorbed_id": None,
                    "confidence": 0.95,
                    "rationale": "buzón",
                },
                {
                    "item": keys[w],
                    "action": "no_parent",
                    "parent_id": None,
                    "parent_name": None,
                    "survivor_id": None,
                    "absorbed_id": None,
                    "confidence": 0.9,
                    "rationale": "empresa independiente",
                },
                {
                    "item": keys[otra],
                    "action": "set_parent",
                    "parent_id": None,
                    "parent_name": "Holding Z",
                    "survivor_id": None,
                    "absorbed_id": None,
                    "confidence": 0.8,
                    "rationale": "filial del holding",
                },
                {
                    "item": keys[child2],
                    "action": "keep_parent",
                    "parent_id": None,
                    "parent_name": None,
                    "survivor_id": None,
                    "absorbed_id": None,
                    "confidence": 0.9,
                    "rationale": "correcto",
                },
                {
                    "item": keys[junk2],
                    "action": "delete",
                    "parent_id": None,
                    "parent_name": None,
                    "survivor_id": None,
                    "absorbed_id": None,
                    "confidence": 0.99,
                    "rationale": "buzón",
                },
            ]
        }
    )
    stats = CuratorStats()
    apply_decisions(conn, 1, items, parse_decisions(content, items), stats)
    # junk2 tiene un hijo: el curador la deja como raíz pineada en vez de borrarla (cleared=2).
    assert (
        stats.set,
        stats.created,
        stats.cleared,
        stats.deleted,
        stats.kept,
        stats.dropped,
        stats.errors,
    ) == (2, 1, 2, 1, 1, 0, 0)
    assert _parent(conn, junk2) == (None, "curator") and _parent(conn, child3)[0] == junk2
    assert _parent(conn, child) == (uni, "curator")  # pineado por el curador
    assert (
        conn.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :j"), {"j": junk}
        ).scalar_one()
        == 0
    )
    assert _parent(conn, w) == (None, "curator")
    assert _parent(conn, child2) == (w, "curator")
    new_parent, src = _parent(conn, otra)
    assert src == "curator" and new_parent is not None
    name = conn.execute(
        text("SELECT display_name, metadata->>'created_by' FROM mod_identidades WHERE id = :p"),
        {"p": new_parent},
    ).one()
    assert tuple(name) == ("Holding Z", "curator")
    assert any(
        c.method == "curator" and c.target_id == uni for c in list_claims(conn, 1, source_id=child)
    )
    # Re-correr: los pineados salen del expediente.
    again = {it.identity_id for it in collect_items(conn, 1, scope="all")}
    assert not ({child, otra, w, child2} & again)


def test_apply_merge_y_umbral(conn: Any) -> None:
    a = _mk(conn, "organizacion", "Anthropic")
    b = _mk(conn, "organizacion", "Anthropic, PBC")
    cand = int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades_merge_candidates "
                "(user_id, identity_a_id, identity_b_id, reason, score) "
                "VALUES (1, :a, :b, 'fuzzy', 0.7) RETURNING id"
            ),
            {"a": a, "b": b},
        ).scalar_one()
    )
    items = collect_items(conn, 1, scope="candidates")
    assert [it.key for it in items] == [f"merge:{cand}"]
    low = json.dumps(
        {
            "decisions": [
                {
                    "item": f"merge:{cand}",
                    "action": "merge",
                    "parent_id": None,
                    "parent_name": None,
                    "survivor_id": a,
                    "absorbed_id": b,
                    "confidence": 0.5,
                    "rationale": "?",
                }
            ]
        }
    )
    stats = CuratorStats()
    apply_decisions(conn, 1, items, parse_decisions(low, items), stats)
    assert stats.dropped == 1 and stats.merged == 0  # bajo el umbral: no funde
    ok = json.dumps(
        {
            "decisions": [
                {
                    "item": f"merge:{cand}",
                    "action": "merge",
                    "parent_id": None,
                    "parent_name": None,
                    "survivor_id": a,
                    "absorbed_id": b,
                    "confidence": 0.95,
                    "rationale": "razón social",
                }
            ]
        }
    )
    stats = CuratorStats()
    apply_decisions(conn, 1, items, parse_decisions(ok, items), stats)
    assert stats.merged == 1
    assert (
        conn.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :b"), {"b": b}
        ).scalar_one()
        == 0
    )


@pytest.mark.asyncio
async def test_run_curator_dry_run_no_aplica_y_registra_llm_call() -> None:
    with connection() as c:
        child = _mk(c, "organizacion", "Área Q")
        uni = _mk(c, "organizacion", "Universidad X")
        claim_and_derive_parent(
            c,
            1,
            child_id=child,
            target_id=uni,
            target_name="",
            confidence=0.95,
            method=METHOD_LLM,
            inbox_id=1,
        )
    seen: list[str] = []

    async def ask(prompt: str) -> CodexRun:
        seen.append(prompt)
        return _run(
            json.dumps(
                {
                    "decisions": [
                        {
                            "item": f"parent:{child}",
                            "action": "keep_parent",
                            "parent_id": None,
                            "parent_name": None,
                            "survivor_id": None,
                            "absorbed_id": None,
                            "confidence": 0.9,
                            "rationale": "ok",
                        }
                    ]
                }
            )
        )

    stats = await run_curator(1, scope="weak", dry_run=True, ask=ask)
    assert stats.items == 1 and stats.batches == 1 and stats.decisions == 1 and stats.kept == 0
    assert "Área Q" in seen[0] and "NUNCA" in seen[0]
    with connection() as c:
        assert _parent(c, child)[1] == "derived"  # dry-run: sigue sin pinear
        n = c.execute(
            text("SELECT count(*) FROM llm_calls WHERE purpose = 'identidades_curate'")
        ).scalar_one()
        assert n == 1
    stats = await run_curator(1, scope="weak", ask=ask)
    assert stats.kept == 1
    with connection() as c:
        assert _parent(c, child)[1] == "curator"


def _mention(conn: Any, ident: int, name: str, n: int = 3) -> None:
    for _ in range(n):
        conn.execute(
            text(
                "INSERT INTO mod_identidades_mentions (user_id, source_inbox_ids, evidence, "
                "mentioned_name, mentioned_kind, confidence, resolved_kind, resolved_identity_id, "
                "resolution_method) VALUES (1, ARRAY[1]::bigint[], 'x', :n, 'organizacion', 0.9, "
                "'organizacion', :i, 'exact_name')"
            ),
            {"i": ident, "n": name},
        )


def _decide(key: str, action: str, **kw: Any) -> str:
    d = {
        "item": key,
        "action": action,
        "parent_id": None,
        "parent_name": None,
        "survivor_id": None,
        "absorbed_id": None,
        "confidence": 0.95,
        "rationale": "r",
    }
    d.update(kw)
    return json.dumps({"decisions": [d]})


def test_containment_pairs_entran_como_items_y_se_recuerdan(conn: Any) -> None:
    """«Salesiana» ⊂ «Pontificia Universidad Salesiana» (lado corto de 1 token, que el barrido de
    health no propone): con menciones suficientes entra como par al curador; si decide que no son
    la misma, queda un candidato rechazado y no se re-pregunta; si decide merge, se funden."""
    corta = _mk(conn, "organizacion", "Salesiana")
    larga = _mk(conn, "organizacion", "Pontificia Universidad Salesiana")
    pobre = _mk(conn, "organizacion", "Universidad")  # 0 menciones → no se propone
    _mention(conn, corta, "Salesiana")
    items = collect_items(conn, 1, scope="candidates")
    assert [it.key for it in items] == [f"pair:{corta}:{larga}"]
    assert pobre not in {it.payload["a"]["id"] for it in items}
    stats = CuratorStats()
    no = _decide(f"pair:{corta}:{larga}", "no_parent", confidence=0.9)
    apply_decisions(conn, 1, items, parse_decisions(no, items), stats)
    assert stats.skipped == 1
    row = conn.execute(
        text(
            "SELECT status, decided_by, reason FROM mod_identidades_merge_candidates "
            "WHERE identity_a_id = :a AND identity_b_id = :b"
        ),
        {"a": corta, "b": larga},
    ).one()
    assert tuple(row) == ("rejected", "curator", "containment")
    assert collect_items(conn, 1, scope="candidates") == []  # no se re-pregunta

    # Merge directo (sin fila previa): se funden.
    otra = _mk(conn, "organizacion", "Sideral")
    larga2 = _mk(conn, "organizacion", "Sideral Development Foundation")
    _mention(conn, otra, "Sideral")
    items = collect_items(conn, 1, scope="candidates")
    assert [it.key for it in items] == [f"pair:{otra}:{larga2}"]
    stats = CuratorStats()
    yes = _decide(f"pair:{otra}:{larga2}", "merge", survivor_id=larga2, absorbed_id=otra)
    apply_decisions(conn, 1, items, parse_decisions(yes, items), stats)
    assert stats.merged == 1
    gone = conn.execute(
        text("SELECT count(*) FROM mod_identidades WHERE id = :i"), {"i": otra}
    ).scalar_one()
    assert gone == 0


def test_pair_set_parent_cuelga_la_hija_y_cierra_el_par(conn: Any) -> None:
    """«Google» ⊂ «Google Cloud Platform»: codex responde set_parent(parent_id=Google) → la larga
    cuelga de la corta (pineada, reclamo curator) y el par queda como NO duplicado."""
    google = _mk(conn, "organizacion", "Google")
    gcp = _mk(conn, "organizacion", "Google Cloud Platform")
    _mention(conn, google, "Google")
    items = collect_items(conn, 1, scope="candidates")
    assert [it.key for it in items] == [f"pair:{google}:{gcp}"]
    stats = CuratorStats()
    dec = _decide(f"pair:{google}:{gcp}", "set_parent", parent_id=google, confidence=0.97)
    apply_decisions(conn, 1, items, parse_decisions(dec, items), stats)
    assert (stats.set, stats.dropped) == (1, 0)
    assert _parent(conn, gcp) == (google, "curator")
    row = conn.execute(
        text(
            "SELECT status, reason FROM mod_identidades_merge_candidates "
            "WHERE identity_a_id = :a AND identity_b_id = :b"
        ),
        {"a": google, "b": gcp},
    ).one()
    assert tuple(row) == ("rejected", "containment")
    assert collect_items(conn, 1, scope="candidates") == []


def test_same_core_y_contencion_por_nucleo(conn: Any) -> None:
    """«Anthropic, PBC» ≡ «Anthropic» (mismo núcleo, sin mínimo de menciones) y la contención se
    evalúa sobre el núcleo (sin puntuación): «Google, Inc.» ⊂ «Google Cloud Platform»."""
    pbc = _mk(conn, "organizacion", "Anthropic, PBC")
    anth = _mk(conn, "organizacion", "Anthropic")
    conn.execute(
        text("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :c"),
        {"p": pbc, "c": anth},
    )
    google = _mk(conn, "organizacion", "Google, Inc.")
    gcp = _mk(conn, "organizacion", "Google Cloud Platform")
    _mention(conn, google, "Google")
    items = collect_items(conn, 1, scope="candidates")
    keys = {it.key: it.payload["reason"] for it in items}
    assert keys == {f"pair:{pbc}:{anth}": "same_core", f"pair:{google}:{gcp}": "containment"}
    assert items[0].payload["reason"] == "same_core"  # primero lo casi seguro
    stats = CuratorStats()
    dec = _decide(f"pair:{pbc}:{anth}", "merge", survivor_id=anth, absorbed_id=pbc)
    apply_decisions(conn, 1, items, parse_decisions(dec, items), stats)
    assert stats.merged == 1
    row = conn.execute(
        text("SELECT aliases, parent_identity_id FROM mod_identidades WHERE id = :a"), {"a": anth}
    ).one()
    assert "Anthropic, PBC" in list(row[0]) and row[1] is None  # queda como alias, sin auto-padre


def test_merge_desde_item_de_padre_se_aplica(conn: Any) -> None:
    """El caso Anthropic: el ítem era de padre («Anthropic» colgando de «Anthropic, PBC»), codex
    respondió merge y antes se descartaba. Ahora se funde y la razón de cualquier descarte queda
    visible («dropped:<razón>»)."""
    pbc = _mk(conn, "organizacion", "Anthropic, PBC")
    anth = _mk(conn, "organizacion", "Anthropic")
    claim_and_derive_parent(
        conn,
        1,
        child_id=anth,
        target_id=pbc,
        target_name="",
        confidence=0.95,
        method=METHOD_LLM,
        inbox_id=1,
    )
    items = [it for it in collect_items(conn, 1, scope="weak") if it.identity_id == anth]
    assert items and items[0].kind == "parent"
    stats = CuratorStats()
    dec = _decide(items[0].key, "merge", survivor_id=anth, absorbed_id=pbc)
    apply_decisions(conn, 1, items, parse_decisions(dec, items), stats)
    assert (stats.merged, stats.dropped) == (1, 0)
    assert (
        conn.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :p"), {"p": pbc}
        ).scalar_one()
        == 0
    )
    # merge con ids ajenos al ítem → descartada con razón
    otra = _mk(conn, "organizacion", "Otra")
    otra2 = _mk(conn, "organizacion", "Otra 2")
    claim_and_derive_parent(
        conn,
        1,
        child_id=otra,
        target_id=anth,
        target_name="",
        confidence=0.95,
        method=METHOD_LLM,
        inbox_id=2,
    )
    items = [it for it in collect_items(conn, 1, scope="weak") if it.identity_id == otra]
    stats = CuratorStats()
    dec = _decide(items[0].key, "merge", survivor_id=anth, absorbed_id=otra2)
    apply_decisions(conn, 1, items, parse_decisions(dec, items), stats)
    assert stats.dropped == 1 and stats.applied[0]["result"] == "dropped:merge_ids"


def test_scope_tree_ve_todo_el_arbol_con_indice(conn: Any) -> None:
    uni = _mk(conn, "organizacion", "Universidad X")
    dep = _mk(conn, "organizacion", "Departamento Q")
    _mk(conn, "persona", "Ana")
    conn.execute(
        text(
            "UPDATE mod_identidades SET parent_identity_id = :p, "
            "metadata = jsonb_set(metadata, '{parent_source}', '\"curator\"') WHERE id = :c"
        ),
        {"p": uni, "c": dep},
    )
    conn.execute(
        text(
            "INSERT INTO mod_identidades_identifiers (user_id, identity_id, platform, kind, value, "
            "value_norm) VALUES (1, :u, 'email', 'domain', 'unix.edu', 'unix.edu')"
        ),
        {"u": uni},
    )
    items = collect_items(conn, 1, scope="tree")
    by = {it.identity_id: it for it in items}
    assert set(by) == {uni, dep}  # pineadas incluidas; personas no
    assert by[dep].kind == "node" and by[dep].payload["pinned"] is True
    assert by[dep].payload["parent_id"] == uni and by[uni].payload["domains"] == ["unix.edu"]
    prompt = build_prompt(items, index=[(it.identity_id, it.payload["name"]) for it in items])
    assert "ÍNDICE COMPLETO" in prompt and f"{uni}: Universidad X" in prompt
    assert "MODO ÁRBOL" in prompt
    # Regla general producto≠empresa (Steam → Valve se cuelga, no se funde) y alias conservados.
    assert "NO es la empresa" in prompt and "quedan como alias" in prompt
    assert "subsidiaria" in prompt  # empresa adquirida = organización colgada, no producto
