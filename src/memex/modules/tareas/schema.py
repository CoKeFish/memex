"""`TareaItem` — la forma de una tarea extraída de un mensaje (extraction_schema de tareas).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con lo mínimo de una tarea:
qué hay que hacer (`title`), para cuándo (`due_on`, opcional: muchas tareas no traen fecha), con
qué urgencia (`priority`) y a qué asunto parece pertenecer (`asunto_hint`: el nombre del curso,
cohorte o trámite tal como lo dice el mensaje; se resuelve después contra los asuntos reales).
`extra="forbid"`: un campo inventado por el LLM invalida el item (mitigación de alucinación).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import ConfigDict, field_validator

from memex.modules.contract import ExtractionItem

Priority = Literal["alta", "normal", "baja"]
#: Cuándo vence cuando el mensaje NO da fecha pero sí la ata a algo: `proxima_sesion` = «para la
#: próxima clase/sesión/reunión» del curso o programa; la fecha se resuelve después contra el
#: calendario del asunto (`extract._next_session`). `mismo_dia` = se gana o se pierde ese mismo
#: día («al primero que responda», «hoy», «antes de que termine la llamada»). `una_semana` = una
#: dinámica o consigna sin plazo dicho: vale la semana y después ya no («dinámica de la semana»,
#: «manden sus fotos»). Sin esto las dinámicas quedaban pendientes para siempre (2026-09-20).
DueKind = Literal["", "proxima_sesion", "mismo_dia", "una_semana"]
#: `tarea` (lo normal) o `recurso`: un mensaje que es SOLO un enlace/archivo de un proyecto ya
#: mencionado (la plantilla de Canva): no crea tarea, se guarda como link del proyecto
#: (`parent_hint`).
#: `hecha`: un mensaje PROPIO que es la entrega o la confirmación de una tarea pedida antes: la
#: completa (o la registra ya completada) en vez de crear un pendiente.
#: `dato`: un hecho pequeño que hace falta tener a mano (código de clase, enlace de acceso, salón,
#: número de expediente): va a los DATOS del asunto, no es tarea.
ItemKind = Literal["tarea", "recurso", "hecha", "dato"]
#: De qué clase de cosa es la tarea (ver `tareas.module.CATEGORIES`). `seguridad` = un aviso de
#: seguridad de una cuenta propia (intento de acceso, contraseña, 2FA, dispositivo nuevo): se
#: captura «por si acaso» aunque sea condicional, con prioridad alta y plazo corto.
Category = Literal["seguridad", "entrega", "tramite", "pago", "inscripcion", "dinamica", "otro"]
MAX_LINKS = 5


class TareaItem(ExtractionItem):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    description: str = ""
    due_on: date | None = None
    due_kind: DueKind = ""
    priority: Priority = "normal"
    asunto_hint: str = ""
    #: Nombre corto del proyecto o entrega mayor del que esta tarea es un PASO («Proyecto de
    #: Canva»);
    #: '' si la tarea es suelta. El sistema agrupa los pasos bajo una tarea padre.
    parent_hint: str = ""
    #: Puntos que da una dinámica o reto de un programa («suma 40 puntos», «por 70 puntos»); None
    #: si el mensaje no habla de puntos. Se guardan en `metadata.puntos` y marcan la tarea como
    #: dinámica del programa.
    points: int | None = None
    #: URLs de los mensajes que hacen falta para hacerla (plantilla, video, sitio, documento).
    links: tuple[str, ...] = ()
    kind: ItemKind = "tarea"
    #: Solo para `kind='dato'`: el dato tal cual (el código, el número, el nombre).
    value: str = ""
    category: Category = "otro"

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, v: object) -> str:
        s = " ".join(str(v or "").split())
        if not s:
            raise ValueError("title vacío")
        return s

    @field_validator("due_on", mode="before")
    @classmethod
    def _parse_due(cls, v: object) -> date | None:
        """YYYY-MM-DD o nada. Una fecha que el LLM no pudo fijar ('', 'null', 'pronto') es None:
        una tarea sin fecha vale más que descartarla por la fecha."""
        if v is None or isinstance(v, date):
            return v
        s = str(v).strip()
        if not s or s.lower() in ("null", "none"):
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None

    @field_validator("points", mode="before")
    @classmethod
    def _parse_points(cls, v: object) -> int | None:
        """40, "40" o "40 puntos" → 40; vacío, null o algo raro → None (nunca invalida el item)."""
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v if v >= 0 else None
        digits = "".join(ch for ch in str(v) if ch.isdigit())
        return int(digits) if digits else None

    @field_validator("links", mode="before")
    @classmethod
    def _clean_links(cls, v: object) -> tuple[str, ...]:
        """Solo http(s), sin repetir, tope MAX_LINKS; cualquier otra cosa se ignora."""
        if v is None:
            return ()
        raw = v if isinstance(v, list | tuple) else [v]
        out: list[str] = []
        for item in raw:
            s = str(item or "").strip()
            if s.lower().startswith(("http://", "https://")) and s not in out:
                out.append(s)
        return tuple(out[:MAX_LINKS])

    @field_validator("kind", mode="before")
    @classmethod
    def _norm_kind(cls, v: object) -> str:
        s = str(v or "tarea").strip().lower()
        if s in ("recurso", "resource", "link", "enlace"):
            return "recurso"
        if s in ("hecha", "hecho", "done", "completada", "completed", "entregada", "submitted"):
            return "hecha"
        if s in ("dato", "data", "fact", "codigo", "código", "code"):
            return "dato"
        return "tarea"

    @field_validator("due_kind", mode="before")
    @classmethod
    def _norm_due_kind(cls, v: object) -> str:
        s = str(v or "").strip().lower().replace(" ", "_").replace("ó", "o").replace("í", "i")
        if s in ("proxima_sesion", "proxima_clase", "next_session"):
            return "proxima_sesion"
        if s in ("mismo_dia", "same_day", "hoy"):
            return "mismo_dia"
        if s in ("una_semana", "semana", "one_week", "week"):
            return "una_semana"
        return ""

    @field_validator("priority", mode="before")
    @classmethod
    def _norm_priority(cls, v: object) -> str:
        s = str(v or "normal").strip().lower()
        if s in ("alta", "high", "urgente", "1"):
            return "alta"
        if s in ("baja", "low", "3"):
            return "baja"
        return "normal"

    @field_validator("description", "asunto_hint", "parent_hint", "value", mode="before")
    @classmethod
    def _clean_text(cls, v: object) -> str:
        return " ".join(str(v or "").split())
