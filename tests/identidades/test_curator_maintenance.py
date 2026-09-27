"""Curador automático: árbol INCREMENTAL (`since` → solo lo tocado, con el índice completo), marcas
de agua en `module_settings.config.curator` y la pasada de mantenimiento (`run_curator_maintenance`,
= job `identidades_curate`): higiene + derivar + acotado + árbol (completo si nunca corrió o
venció; incremental si no)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.curator import (
    collect_items,
    directory_index,
    get_marks,
    run_curator,
    run_curator_maintenance,
    touched_since,
)
from memex.modules.identidades.links import METHOD_LLM, claim_and_derive_parent
from memex.scheduler.jobs import all_jobs as registry
from memex.webcontext._codex_subprocess import CodexRun
from memex.webcontext.client import WebContextUsage


def _mk(conn: Any, name: str, kind: str = "organizacion") -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) VALUES (1, :k, :n) "
                "RETURNING id"
            ),
            {"k": kind, "n": name},
        ).scalar_one()
    )


def _age(conn: Any, ident: int, days: int) -> None:
    conn.execute(
        text(
            "UPDATE mod_identidades SET created_at = NOW() - make_interval(days => :d), "
            "updated_at = NOW() - make_interval(days => :d) WHERE id = :i"
        ),
        {"d": days, "i": ident},
    )


def _run(content: str) -> CodexRun:
    return CodexRun(
        last_message=content, usage=WebContextUsage(input_tokens=1, output_tokens=1), latency_ms=1
    )


def test_tree_incremental_solo_lo_tocado_con_indice_completo(conn: Any) -> None:
    vieja = _mk(conn, "Empresa Vieja")
    hija_vieja = _mk(conn, "Área Vieja")
    _age(conn, vieja, 10)
    _age(conn, hija_vieja, 10)
    since = datetime.now(UTC) - timedelta(days=1)
    nueva = _mk(conn, "Empresa Nueva")  # creada después de la marca
    # reclamo nuevo sobre una entrada vieja → también entra (evidencia nueva)
    claim_and_derive_parent(
        conn,
        1,
        child_id=hija_vieja,
        target_id=vieja,
        target_name="",
        confidence=0.95,
        method=METHOD_LLM,
        inbox_id=1,
    )
    assert touched_since(conn, 1, since) == {nueva, hija_vieja, vieja}
    # `vieja` entra como destino del reclamo; `hija_vieja` como origen (además updated por derive)
    items = collect_items(conn, 1, scope="tree", since=since)
    assert {it.identity_id for it in items} == {nueva, hija_vieja, vieja}
    # una marca posterior a todo → delta vacío
    assert collect_items(conn, 1, scope="tree", since=datetime.now(UTC)) == []
    # el índice es SIEMPRE el directorio completo
    assert {i for i, _ in directory_index(conn, 1)} == {vieja, hija_vieja, nueva}


@pytest.mark.asyncio
async def test_marcas_y_mantenimiento_full_luego_incremental() -> None:
    with connection() as c:
        acme = _mk(c, "Acme")
        prod = _mk(c, "Acme Cloud")
        assert get_marks(c, 1) == {"last_tree_at": None, "last_full_tree_at": None}
    seen: list[int] = []

    async def ask(prompt: str) -> CodexRun:
        seen.append(prompt.count('"item":'))
        # solo la primera llamada propone algo (colgar el producto); el resto, nada
        if len(seen) == 1:
            return _run(
                json.dumps(
                    {
                        "decisions": [
                            {
                                "item": f"node:{prod}",
                                "action": "set_parent",
                                "parent_id": acme,
                                "parent_name": None,
                                "survivor_id": None,
                                "absorbed_id": None,
                                "kind": None,
                                "confidence": 0.95,
                                "rationale": "producto de Acme",
                            }
                        ]
                    }
                )
            )
        return _run(json.dumps({"decisions": []}))

    # 1ª pasada de mantenimiento: nunca corrió → árbol COMPLETO (2 rondas: la 2ª no cambia nada)
    ms = await run_curator_maintenance(1, ask=ask)
    assert ms.errors == 0 and ms.tree_mode == "full"
    assert ms.tree["set"] == 1 and ms.tree["rounds"] == 2
    with connection() as c:
        marks = get_marks(c, 1)
        assert marks["last_tree_at"] is not None and marks["last_full_tree_at"] is not None
        parent = c.execute(
            text("SELECT parent_identity_id FROM mod_identidades WHERE id = :p"), {"p": prod}
        ).scalar()
    assert parent == acme
    # 2ª pasada el mismo día: árbol INCREMENTAL; nada tocado desde la marca → 0 ítems, sin llamada
    # del árbol (el acotado sí mira a «Acme»: raíz con hijo y sin menciones = sospechosa).
    calls_before = len(seen)
    ms2 = await run_curator_maintenance(1, ask=ask)
    assert ms2.tree_mode == "incremental" and ms2.tree["items"] == 0
    assert ms2.targeted["items"] == 1 and ms2.targeted["decisions"] == 0
    assert len(seen) == calls_before + 1
    # una entrada nueva → el incremental la ve (y solo a ella), con el índice completo
    with connection() as c:
        nueva = _mk(c, "Nueva Corp")
    ms3 = await run_curator_maintenance(1, ask=ask)
    assert ms3.tree_mode == "incremental" and ms3.tree["items"] == 1
    assert seen[-1] == 1  # un solo ítem en el prompt…
    del nueva
    # …y si la última completa venció, vuelve a ser completa
    with connection() as c:
        c.execute(
            text(
                "UPDATE module_settings SET config = jsonb_set(config, "
                "'{curator,last_full_tree_at}', to_jsonb(CAST(:v AS TEXT))) "
                "WHERE user_id = 1 AND module_slug = 'identidades'"
            ),
            {"v": (datetime.now(UTC) - timedelta(days=8)).isoformat()},
        )
    ms4 = await run_curator_maintenance(1, ask=ask)
    assert ms4.tree_mode == "full" and ms4.tree["items"] == 3


def test_job_registrado_y_apagado_por_default() -> None:
    jobs = registry()
    assert "identidades_curate" in jobs and jobs["identidades_curate"].default_interval == "P1D"


def test_run_curator_since_ignora_dry_run_marks(conn: Any) -> None:
    # dry-run no escribe marcas
    import asyncio

    _mk(conn, "Solo")

    async def ask(prompt: str) -> CodexRun:
        return _run(json.dumps({"decisions": []}))

    asyncio.run(run_curator(1, scope="tree", dry_run=True, ask=ask))
    assert get_marks(conn, 1)["last_tree_at"] is None
