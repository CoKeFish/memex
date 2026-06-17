#!/usr/bin/env python
"""Lint repo-local: NADIE fuera del paquete `relations/` muta el grafo en crudo.

El grafo (vértices/aristas/cúmulos) se muta SOLO por el chokepoint `memex.relations.graph_writer`,
que marca `dirty` y propaga a los vecinos (groundwork incremental, ADR-021). Esta regla lo hace
cumplir por máquina: en cualquier archivo de `src/memex/` que NO sea del paquete `relations/`
(es decir, los MÓDULOS, la API y los CLI) está prohibido:

  1. Escribir crudo a una tabla del grafo: INSERT INTO / UPDATE / DELETE FROM sobre
     relation_edges | relation_vertex_state | relation_clusters | relation_cluster_members.
  2. Llamar a las primitivas crudas de aristas: propose_edge / resolve_edge / mark_vertices_dirty
     (son la implementación; los módulos usan graph_writer.add_edge/update_verdict/delete_edge/
     prune_edges/add_vertex/update_vertex/merge_vertices/delete_vertex/reject_override/mark_dirty).

El paquete `relations/` ES la maquinaria del grafo: usa las primitivas directo y queda exento
(decisión del dueño: «frontera de módulos»). Espeja el estilo del ban T20 (regla con exenciones por
ruta). Corre en pre-commit + CI; un módulo/API/CLI que escriba el grafo en crudo NO compila.

Uso: `python scripts/lint_no_raw_graph_writes.py` (exit 1 si hay violaciones).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "memex"
#: El paquete sancionado (la implementación del grafo) queda exento.
_EXEMPT_PREFIX = _SRC / "relations"

_GRAPH_TABLES = "relation_edges|relation_vertex_state|relation_clusters|relation_cluster_members"
_RAW_WRITE = re.compile(rf"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:{_GRAPH_TABLES})\b")
#: Llamada a una primitiva cruda (con o sin prefijo de módulo, p.ej. `edges.propose_edge(`).
_RAW_PRIMITIVE = re.compile(r"\b(propose_edge|resolve_edge|mark_vertices_dirty)\s*\(")


def _violations(path: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if _RAW_WRITE.search(line):
            out.append((n, "escritura cruda a una tabla del grafo"))
        if _RAW_PRIMITIVE.search(line):
            out.append((n, "llamada a una primitiva cruda (usar memex.relations.graph_writer)"))
    return out


def main() -> int:
    bad: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if _EXEMPT_PREFIX in path.parents:
            continue
        for n, why in _violations(path):
            bad.append(f"{path.relative_to(_SRC.parent.parent)}:{n}: {why}")
    if bad:
        print("Escrituras crudas del grafo fuera de relations/ (usá memex.relations.graph_writer):")
        for b in bad:
            print(f"  {b}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
