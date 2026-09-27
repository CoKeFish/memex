"""Agenda «qué sigue» del servidor: misma regla que la ventana del asunto."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from sqlalchemy import text

from memex.db import connection
from memex.modules.tareas import module as tareas
from memex.modules.tareas.agenda import agenda


def _asunto(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                    "VALUES (1, :n, 'programa', 'abierto', ARRAY[]::text[]) RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def test_agenda_orders_by_date_and_rolls_steps_into_the_project() -> None:
    today = date(2026, 9, 13)
    curso = _asunto(f"Curso {uuid4().hex[:5]}")
    with connection() as c:
        proy = tareas.add(
            c, 1, title="Proyecto de Instagram", asunto=curso, metadata={"is_parent": True}
        )
        s1 = tareas.add(c, 1, title="Primer post", due_on=date(2026, 9, 15), asunto=curso)
        s2 = tareas.add(c, 1, title="Segundo post", due_on=date(2026, 9, 23), asunto=curso)
        s3 = tareas.add(c, 1, title="Paso cancelado", due_on=date(2026, 12, 1), asunto=curso)
        for s in (s1, s2, s3):
            tareas.update(c, 1, int(s["id"]), parent=int(proy["id"]))
        tareas.set_status(c, 1, int(s1["id"]), "completada")
        tareas.set_status(c, 1, int(s3["id"]), "cancelada")
        tareas.add(
            c,
            1,
            title="Llevar papel",
            due_on=date(2026, 9, 10),
            asunto=curso,
            metadata={"due_source": "proxima_sesion"},
        )
        tareas.add(c, 1, title="Leer capítulo", due_on=date(2026, 9, 14), asunto=curso)
        tareas.add(c, 1, title="Sin fecha", asunto=curso)
        tareas.add(c, 1, title="Muy lejos", due_on=date(2026, 12, 24), asunto=curso)
        items = agenda(c, 1, today=today, days=14, asunto_id=curso)
    assert [i.title for i in items] == ["Llevar papel", "Leer capítulo", "Proyecto de Instagram"]
    assert items[0].late is True and items[0].detail == "para la próxima clase"
    assert items[1].late is False and items[1].kind == "tarea"
    # entrega = la mayor fecha entre los pasos vivos (el cancelado no cuenta); avance 1 de 2
    assert items[2].kind == "entrega" and items[2].date == date(2026, 9, 23)
    assert items[2].detail == "1 de 2 pasos" and items[2].asunto_id == curso
