"""`memex-asuntos purge --status …`: ensayo sin --apply; con --apply borra los asuntos de ese
estado y sus tareas, y no toca los demás."""

from __future__ import annotations

from typing import Any

from memex.db import connection
from memex.modules.asuntos import store
from memex.modules.asuntos.cli import main
from memex.modules.tareas import module as tareas
from tests.asuntos._seed import asunto


def test_purge_lists_then_deletes_only_that_status(capsys: Any) -> None:
    inactivo = asunto("Semillero viejo", status="inactivo")
    abierto = asunto("Curso vivo", status="abierto")
    # un asunto fusionado dentro del inactivo (redirección) se va con él
    fusionado = asunto("Semillero viejo (alias)", status="inactivo")
    with connection() as c:
        store.merge_asuntos(c, 1, survivor_id=inactivo, absorbed_id=fusionado)
    with connection() as c:
        t = tareas.add(c, 1, title="Tarea del semillero", asunto=inactivo)
        tareas.add(c, 1, title="Tarea del curso", asunto=abierto)
    assert main(["purge", "--status", "inactivo"]) == 0
    out = capsys.readouterr().out
    assert "Semillero viejo" in out and "ensayo" in out
    with connection() as c:
        assert store.get_asunto(c, 1, inactivo) is not None
    assert main(["purge", "--status", "inactivo", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "borrados 1 asuntos y 1 tareas (+1 fusionados dentro de ellos)" in out
    with connection() as c:
        assert store.get_asunto(c, 1, inactivo) is None
        assert store.get_asunto(c, 1, fusionado) is None
        assert store.get_asunto(c, 1, abierto) is not None
        assert [x["title"] for x in tareas.list_tareas(c, 1, status=None, limit=50)] == [
            "Tarea del curso"
        ]
        try:
            tareas.get(c, 1, int(t["id"]))
            raise AssertionError("la tarea del asunto borrado sigue")
        except tareas.TareaNotFoundError:
            pass
    assert main(["purge", "--status", "fax"]) == 2
