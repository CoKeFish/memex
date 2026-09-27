"""Agenda «qué sigue» del lado servidor: la MISMA regla que la ventana del asunto en el dashboard
(`frontend/src/lib/asunto-story.ts: agendaOf`), para el resumen matutino y el asistente.

Lo pendiente con fecha, de lo más próximo a lo más lejano: una tarea suelta por su vencimiento;
un proyecto por su ENTREGA (la mayor fecha entre la suya y la de sus pasos no cancelados) con su
avance («3 de 9 pasos»); los pasos NO se listan uno por uno. Lo que pasó y sigue pendiente queda
marcado `late`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.engine import Connection

from memex.modules.tareas.module import STATUS_CANCELADA, STATUS_PENDIENTE, list_tareas


@dataclass(frozen=True)
class AgendaItem:
    id: int
    title: str
    date: date
    kind: str  #: 'tarea' | 'entrega'
    late: bool
    #: «3 de 9 pasos», «para la próxima clase» o ''.
    detail: str
    asunto_id: int | None
    asunto_name: str | None
    priority: str


def _due(t: dict[str, Any]) -> date | None:
    raw = t.get("due_on")
    return date.fromisoformat(str(raw)) if raw else None


def agenda(
    conn: Connection,
    user_id: int,
    *,
    today: date,
    days: int = 7,
    asunto_id: int | None = None,
) -> list[AgendaItem]:
    """Pendientes con fecha hasta `today + days` (más lo atrasado), ordenados por fecha."""
    all_rows = list_tareas(conn, user_id, status=None, asunto_id=asunto_id, limit=5000)
    ids = {int(t["id"]) for t in all_rows}
    steps_of: dict[int, list[dict[str, Any]]] = {}
    for t in all_rows:
        pid = t.get("parent_id")
        if pid is not None and int(pid) in ids and t["status"] != STATUS_CANCELADA:
            steps_of.setdefault(int(pid), []).append(t)
    limit = today + timedelta(days=days)
    out: list[AgendaItem] = []
    for t in all_rows:
        if t["status"] != STATUS_PENDIENTE:
            continue
        pid = t.get("parent_id")
        if pid is not None and int(pid) in ids and t["status"] != STATUS_CANCELADA:
            continue  # un paso: entra por la entrega de su proyecto
        meta = t.get("metadata") or {}
        steps = steps_of.get(int(t["id"]), [])
        is_project = bool(meta.get("is_parent")) or bool(steps)
        if is_project:
            dues = [d for d in [_due(t), *(_due(s) for s in steps)] if d is not None]
            if not dues:
                continue
            due = max(dues)
            done = sum(1 for s in steps if s["status"] == "completada")
            kind, detail = "entrega", (f"{done} de {len(steps)} pasos" if steps else "")
        else:
            d = _due(t)
            if d is None:
                continue
            due = d
            kind = "tarea"
            detail = "para la próxima clase" if meta.get("due_source") == "proxima_sesion" else ""
        late = due < today
        if not late and due > limit:
            continue
        out.append(
            AgendaItem(
                id=int(t["id"]),
                title=str(t["title"]),
                date=due,
                kind=kind,
                late=late,
                detail=detail,
                asunto_id=(int(t["asunto_id"]) if t.get("asunto_id") is not None else None),
                asunto_name=(str(t["asunto_name"]) if t.get("asunto_name") else None),
                priority=str(t.get("priority") or "normal"),
            )
        )
    out.sort(key=lambda i: (i.date, i.id))
    return out
