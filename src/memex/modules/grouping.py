"""Etapa B del orquestador: agrupado de módulos para la extracción (ADR-015 §2, perilla
`batching_policy`).

Todo puro (sin DB ni LLM, igual que `routing.py`):

1. `plan_groups` — particiona los módulos elegidos (ya topo-ordenados) en grupos según la
   policy (`per_module`/`grouped`/`all`) y `group_size`, **sin co-agrupar un módulo con una
   dependencia transitiva suya** (el dependiente necesita el output persistido de su dep, que va
   en un grupo anterior por el topo-orden). `_transitive_deps` es el cierre que lo respalda.
2. Prompt agrupado (`GROUPED_SYSTEM_PROMPT` + `build_grouped_user_content` + `parse_grouped_items`)
   para una sola llamada LLM que extrae para varios módulos a la vez. Cada bloque por módulo lleva
   su `interest`, su JSON Schema y su `extraction_prompt` hand-tuned (se preserva el tuning).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from memex.modules.contract import InterestModule


def _transitive_deps(slug: str, active: Mapping[str, InterestModule]) -> set[str]:
    """Cierre transitivo de `depends_on` de `slug`, siguiendo solo slugs activos (mismo criterio
    skip-inactive que `resolve_order`). NO incluye `slug`."""
    seen: set[str] = set()
    stack = [slug]
    while stack:
        mod = active.get(stack.pop())
        if mod is None:
            continue
        for dep in mod.depends_on:
            if dep in active and dep not in seen:
                seen.add(dep)
                stack.append(dep)
    seen.discard(slug)
    return seen


def plan_groups(
    chosen: Sequence[str],
    active: Mapping[str, InterestModule],
    policy: str,
    group_size: int,
) -> list[list[str]]:
    """Particiona `chosen` (YA topo-ordenado) en grupos de extracción.

    - `per_module` → un grupo por módulo (comportamiento por defecto).
    - `grouped` → grupos de a lo sumo `group_size` módulos.
    - `all` → un solo grupo (sin tope de tamaño).

    Invariante en `grouped`/`all`: nunca se co-agrupa un módulo con una dependencia transitiva
    suya — al colocar `s`, si el grupo actual ya contiene una dep de `s`, se cierra y se abre uno
    nuevo. Como `chosen` viene topo-ordenado, la dep queda en un grupo ANTERIOR (ya persistida
    cuando corre el dependiente).
    """
    if policy == "per_module":
        return [[s] for s in chosen]
    cap = max(group_size if policy == "grouped" else len(chosen), 1)
    tdeps = {s: _transitive_deps(s, active) for s in chosen}
    groups: list[list[str]] = []
    current: list[str] = []
    for s in chosen:
        if current and (len(current) >= cap or any(d in current for d in tdeps[s])):
            groups.append(current)
            current = []
        current.append(s)
    if current:
        groups.append(current)
    return groups


# --- Prompt de extracción agrupada -------------------------------------------------- #

GROUPED_SYSTEM_PROMPT = (
    "Sos un extractor multi-módulo de datos personales. Te paso varios MÓDULOS (cada uno con su "
    "`slug`, su `interest`, su esquema JSON de item y sus instrucciones) y una lista de mensajes. "
    "Por CADA módulo, extraé SOLO sus items de los mensajes provistos, respetando su esquema y "
    "citando en `source_inbox_ids` únicamente ids reales de los mensajes.\n"
    "Respondé SOLO con un objeto JSON cuyas claves sean los `slug` y cada valor la lista de items "
    'de ese módulo: {"<slug>": [ {item}, ... ], ...}. Si un módulo no tiene items, devolvé [] '
    "para ese slug. No inventes ids, módulos ni datos que no estén en los mensajes."
)


def build_grouped_user_content(modules: Sequence[InterestModule], messages_json: str) -> str:
    """Arma el turno `user` del agrupado: un bloque por módulo (slug + interest + JSON Schema +
    sus instrucciones hand-tuned) + los mensajes del lote."""
    blocks: list[str] = []
    for m in modules:
        schema_json = json.dumps(m.extraction_schema.model_json_schema(), ensure_ascii=False)
        blocks.append(
            f"### Módulo: {m.slug}\n"
            f"Interés: {m.interest}\n"
            f"Esquema del item (JSON Schema):\n{schema_json}\n"
            f"Instrucciones del módulo:\n{m.extraction_prompt}"
        )
    modules_block = "\n\n".join(blocks)
    return f"Módulos:\n{modules_block}\n\nMensajes (JSON):\n{messages_json}"


def parse_grouped_items(content: str, slugs: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Parsea `{"<slug>": [...], ...}` a un dict slug→items. Defensivo como `parse_items`: JSON
    inválido, no-dict, o slug ausente → `[]` para ese slug; elementos no-dict se filtran; claves
    fuera de `slugs` se ignoran. Siempre devuelve una entrada por cada slug pedido."""
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in slugs}
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return out
    if not isinstance(data, dict):
        return out
    for s in slugs:
        raw = data.get(s)
        if isinstance(raw, list):
            out[s] = [item for item in raw if isinstance(item, dict)]
    return out
