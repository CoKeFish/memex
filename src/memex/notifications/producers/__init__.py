"""Productores de avisos: cada uno mira un dominio (agenda, tareas, sistema…) y decide qué avisar.

Contrato: una `ProducerSpec` declara nombre, audiencia, defaults (activo, redacción, `config` con
sus tiempos) y la función `produce(ctx) -> list[Draft]`. Son DETERMINISTAS: leen datos ya
extraídos y arman un borrador con plantilla (`body`) más los hechos estructurados (`facts`). Si el
dueño configura `render=llm`, el servicio le pide a un modelo que redacte a partir de esos hechos
(`llm_brief` dice qué escribir); si la llamada falla, queda la plantilla. La idempotencia es por
`dedup_key` (un borrador cuya clave ya existe en la cola se descarta antes de redactar).

Registro: `_REGISTRY` (dict) + `all_producers()`; agregar un productor = un módulo nuevo acá y una
línea en el registro. Los productores NO abren conexiones: reciben la del ciclo en `ctx.conn`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection


@dataclass(frozen=True)
class ProducerContext:
    conn: Connection
    user_id: int
    #: Ahora en la ZONA DEL USUARIO (aware). Las horas de los ajustes se comparan contra esto.
    now: datetime
    #: Config resuelta (defaults del productor ⊕ fila de la DB).
    config: dict[str, Any]


@dataclass(frozen=True)
class Draft:
    """Un aviso por emitir: plantilla lista + hechos para el redactor LLM."""

    kind: str
    severity: str
    dedup_key: str
    title: str
    body: str
    facts: dict[str, Any] = field(default_factory=dict)
    #: Instrucción para el redactor LLM (vacío = este borrador no se redacta con modelo).
    llm_brief: str = ""
    deep_link: str | None = None
    expires_at: datetime | None = None


ProduceFn = Callable[[ProducerContext], Awaitable[list[Draft]]]


@dataclass(frozen=True)
class ProducerSpec:
    name: str
    audience: str  # "personal" | "sistema"
    description: str
    default_enabled: bool
    default_render: str  # "plantilla" | "llm"
    default_config: dict[str, Any]
    produce: ProduceFn
    #: False = siempre plantilla (p. ej. un recordatorio de evento no gana nada con un modelo).
    llm_allowed: bool = True


def _registry() -> dict[str, ProducerSpec]:
    # Import tardío: los módulos de productores importan `Draft`/`ProducerSpec` de acá.
    from memex.notifications.producers import (
        asuntos,
        eventos,
        pagos,
        renovaciones,
        resumen,
        sistema,
        tareas,
        verificar_cuentas,
    )

    specs = (
        resumen.SPEC,
        eventos.SPEC,
        tareas.SPEC,
        asuntos.SPEC,
        pagos.SPEC,
        renovaciones.SPEC,
        verificar_cuentas.SPEC,
        sistema.SPEC,
    )
    return {s.name: s for s in specs}


def all_producers() -> dict[str, ProducerSpec]:
    """Los productores registrados, por nombre (copia)."""
    return dict(_registry())


__all__ = ["Draft", "ProduceFn", "ProducerContext", "ProducerSpec", "all_producers"]
