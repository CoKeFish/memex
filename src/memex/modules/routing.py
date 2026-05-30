"""Etapa A del orquestador: ruteo (qué módulos se usan) — ADR-015 §2.

Tres piezas, todas puras (sin DB; el LLM se inyecta donde haga falta):

1. `candidates_for_kind` — pre-filtro determinista barato por `consumes_kinds`.
2. `resolve_order` — cierre de `depends_on` + topo-sort (deps antes que dependientes). Saltea
   dependientes cuya dependencia no está activa (`dropped`); ciclo → ValueError.
3. ruteo por LLM (`ROUTING_SYSTEM_PROMPT` + `build_routing_user_content` + `parse_routing`)
   para cuando hay ≥2 candidatos. Con 0/1 candidato el orquestador hace short-circuit (sin LLM).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from memex.core.source import SourceKind
from memex.modules.contract import InterestModule


def candidates_for_kind(
    kind: SourceKind, modules: Sequence[InterestModule]
) -> list[InterestModule]:
    """Módulos que consumen esta categoría de fuente (pre-filtro pre-LLM)."""
    return [m for m in modules if kind in m.consumes_kinds]


@dataclass(frozen=True)
class RouteResult:
    """Orden topológico de los módulos a correr + los salteados por dependencia faltante."""

    order: tuple[str, ...]
    dropped: tuple[str, ...]


def _toposort(survivors: set[str], active: Mapping[str, InterestModule]) -> list[str]:
    """Kahn determinista sobre `survivors` (deps antes que dependientes). Ciclo → ValueError."""
    indeg = {s: 0 for s in survivors}
    adj: dict[str, list[str]] = {s: [] for s in survivors}
    for s in survivors:
        for dep in active[s].depends_on:
            if dep in survivors:
                adj[dep].append(s)
                indeg[s] += 1

    queue = sorted(s for s in survivors if indeg[s] == 0)
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for dependent in sorted(adj[node]):
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                queue.append(dependent)
        queue.sort()

    if len(order) != len(survivors):
        raise ValueError("ciclo en el grafo de dependencias de módulos")
    return order


def resolve_order(chosen: Iterable[str], active: Mapping[str, InterestModule]) -> RouteResult:
    """Expande `chosen` con sus `depends_on` (cierre transitivo) y los ordena topológicamente.

    Un módulo cuya dependencia NO está activa (o no existe) se SALTEA junto con todo lo que
    dependa de él (`dropped`). Un slug elegido que no está activo también se saltea.
    """
    needed: set[str] = set()
    bad: set[str] = set()

    def visit(slug: str) -> None:
        if slug in needed:
            return
        needed.add(slug)
        mod = active.get(slug)
        if mod is None:
            bad.add(slug)
            return
        for dep in mod.depends_on:
            if dep not in active:
                bad.add(slug)
            else:
                visit(dep)

    for slug in chosen:
        if slug in active:
            visit(slug)
        else:
            needed.add(slug)
            bad.add(slug)

    # Propagar el drop: lo que dependa (transitivamente) de algo malo, también cae.
    dropped = set(bad)
    changed = True
    while changed:
        changed = False
        for slug in needed:
            if slug in dropped:
                continue
            mod = active.get(slug)
            if mod is not None and any(dep in dropped for dep in mod.depends_on):
                dropped.add(slug)
                changed = True

    survivors = {s for s in needed if s not in dropped}
    order = _toposort(survivors, active)
    return RouteResult(order=tuple(order), dropped=tuple(sorted(dropped)))


# --- Ruteo por LLM (≥2 candidatos) -------------------------------------------------- #

ROUTING_SYSTEM_PROMPT = (
    "Sos un router de módulos de extracción de datos personales. Te paso un CATÁLOGO de módulos "
    "(cada uno con un `slug` y un `interest` que describe qué datos extrae) y una lista de "
    "mensajes. Decidí qué módulos son RELEVANTES para estos mensajes (cuáles podrían encontrar "
    "datos de su interés).\n"
    'Respondé SOLO con un objeto JSON con esta forma: {"modules": ["<slug>", ...]} usando '
    'slugs del catálogo. Si ninguno aplica, devolvé {"modules": []}.'
)


def build_routing_user_content(catalog: Sequence[tuple[str, str]], messages_json: str) -> str:
    """Arma el turno `user` del ruteo: catálogo (slug + interest) + mensajes."""
    catalog_str = "\n".join(f"- {slug}: {interest}" for slug, interest in catalog)
    return f"Catálogo de módulos:\n{catalog_str}\n\nMensajes (JSON):\n{messages_json}"


def parse_routing(content: str) -> list[str] | None:
    """Parsea `{"modules": [...]}`. Devuelve la lista de slugs (sin filtrar), o None si el JSON
    es inválido o `modules` no es una lista (el caller cae a "todos los candidatos")."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    mods = data.get("modules")
    if not isinstance(mods, list):
        return None
    return [str(m).strip() for m in mods if isinstance(m, str) and str(m).strip()]
