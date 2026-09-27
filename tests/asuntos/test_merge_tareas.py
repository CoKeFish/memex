"""Fundir dos asuntos re-apunta las TAREAS del absorbido al superviviente (2026-09-25: quedaban
colgadas del asunto fundido y la vista mostraba el nombre viejo). Y `relink_orphans` recoge las
que quedaron así antes del arreglo, siguiendo cadenas de fusiones."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store
from memex.modules.tareas.module import add, get, relink_orphans
from tests.asuntos._seed import asunto


def _asunto_of(tid: int) -> int | None:
    with connection() as c:
        return get(c, 1, tid).get("asunto_id")


def test_merge_asuntos_reapunta_las_tareas(conn: Any) -> None:
    a, b = asunto("Programa de Embajadores"), asunto("StarForge (viejo)")
    with connection() as c:
        pend = add(c, 1, title="Descifrar el mensaje", asunto=b)
        hecha = add(c, 1, title="Aportar una palabra", asunto=b)
        c.execute(
            text("UPDATE mod_tareas SET status = 'completada' WHERE id = :i"), {"i": hecha["id"]}
        )
        ajena = add(c, 1, title="Otra cosa", asunto=a)
        assert store.merge_asuntos(c, 1, survivor_id=a, absorbed_id=b)
    assert _asunto_of(pend["id"]) == a and _asunto_of(hecha["id"]) == a
    assert _asunto_of(ajena["id"]) == a
    with connection() as c:
        assert get(c, 1, pend["id"])["asunto_name"] == "Programa de Embajadores"


def test_relink_recoge_tareas_de_asuntos_ya_fundidos(conn: Any) -> None:
    a, b, c3 = asunto("Sobreviviente"), asunto("Fundido"), asunto("Fundido dos veces")
    with connection() as c:
        t1 = add(c, 1, title="Tarea del fundido", asunto=b)
        t2 = add(c, 1, title="Tarea del fundido dos veces", asunto=c3)
        # simula fusiones hechas ANTES del arreglo: el puntero queda viejo
        c.execute(
            text("UPDATE mod_asuntos SET merged_into_id = :s WHERE id = :x"), {"s": a, "x": b}
        )
        c.execute(
            text("UPDATE mod_asuntos SET merged_into_id = :s WHERE id = :x"), {"s": b, "x": c3}
        )
        c.execute(
            text("UPDATE mod_tareas SET asunto_id = :x WHERE id = :t"), {"x": b, "t": t1["id"]}
        )
        c.execute(
            text("UPDATE mod_tareas SET asunto_id = :x WHERE id = :t"), {"x": c3, "t": t2["id"]}
        )
    with connection() as c:
        dry = relink_orphans(c, 1, apply=False)
    assert dry["remerged"] == 2 and _asunto_of(t1["id"]) == b
    with connection() as c:
        st = relink_orphans(c, 1, apply=True)
    assert st["remerged"] == 2
    assert _asunto_of(t1["id"]) == a and _asunto_of(t2["id"]) == a  # la cadena c3→b→a se sigue
    with connection() as c:
        assert relink_orphans(c, 1, apply=True)["remerged"] == 0  # idempotente
