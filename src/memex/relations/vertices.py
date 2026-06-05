"""Proyección de vértices del grafo: lee las tablas `mod_*` de dominio y las normaliza a una lista
uniforme de vértices `(slug, id, label, kind)`, para que las aristas y el front las traten igual.

Solo LEE (ADR-001/ADR-015): cada módulo sigue dueño de su tabla; esta capa no copia ni escribe. El
`slug` es la clave de direccionamiento del grafo (igual que en `Ref`): codifica el subtipo de
identidades (`identidades:person`/`identidades:org`) y apunta al evento CONSOLIDADO de calendar (no
a los crudos). inbox NO es vértice: es procedencia (el `source_inbox_ids` de cada fila), accesible
por drill-down, no un nodo del grafo. Los cúmulos (vértices nativos) se sumarán acá cuando existan.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.relations.edges import Ref


@dataclass(frozen=True)
class Vertex:
    """Un vértice del grafo, proyectado uniformemente desde su tabla de origen."""

    slug: str
    id: int
    label: str
    kind: str

    @property
    def ref(self) -> Ref:
        return Ref(self.slug, self.id)


@dataclass(frozen=True)
class NodeSource:
    """Cómo proyectar una tabla como vértices: su `slug` de grafo, la `table`, la expresión de
    etiqueta (`label_expr`), el `kind` (tipo humano) y un filtro opcional (`where`, p.ej. excluir
    consolidados borrados)."""

    slug: str
    table: str
    label_expr: str
    kind: str
    where: str = ""


#: Registro de proyección: una entrada por TIPO de vértice de dominio. Son literales internos (NO
#: input de usuario) → seguro interpolarlos en el SQL; `user_id`/`id` van por bind. calendar apunta
#: al consolidado; identidades proyecta DOS slugs; inbox NO está (es atributo, no vértice).
NODE_SOURCES: tuple[NodeSource, ...] = (
    NodeSource("finance", "mod_finance_transactions", "counterparty", "transaccion"),
    NodeSource("hackathones", "mod_hackathones_events", "name", "hackaton"),
    NodeSource("calendar", "mod_calendar_consolidated", "title", "evento", where="NOT deleted"),
    NodeSource(
        "identidades:person", "mod_identidades", "display_name", "persona", where="kind = 'persona'"
    ),
    NodeSource(
        "identidades:org",
        "mod_identidades",
        "display_name",
        "organizacion",
        where="kind = 'organizacion'",
    ),
)

_BY_SLUG: dict[str, NodeSource] = {s.slug: s for s in NODE_SOURCES}


def _select_for(src: NodeSource, *, by_id: bool = False) -> str:
    """SELECT que proyecta `src` a la tupla canónica `(slug, id, label, kind)` del user."""
    extra = f" AND {src.where}" if src.where else ""
    if by_id:
        extra += " AND id = :id"
    return (
        f"SELECT '{src.slug}' AS slug, id, ({src.label_expr})::text AS label, "
        f"'{src.kind}' AS kind FROM {src.table} WHERE user_id = :uid{extra}"
    )


def list_vertices(
    conn: Connection, user_id: int, *, slugs: tuple[str, ...] | None = None
) -> list[Vertex]:
    """Todos los vértices del user (o solo los de `slugs`), proyectados uniformemente: UNION ALL de
    un SELECT por tipo de vértice, ordenado por `(slug, id)`."""
    sources = NODE_SOURCES if slugs is None else tuple(_BY_SLUG[s] for s in slugs if s in _BY_SLUG)
    if not sources:
        return []
    sql = " UNION ALL ".join(_select_for(s) for s in sources) + " ORDER BY slug, id"
    rows = conn.execute(text(sql), {"uid": user_id}).mappings().all()
    return [Vertex(str(r["slug"]), int(r["id"]), str(r["label"]), str(r["kind"])) for r in rows]


def get_vertex(conn: Connection, user_id: int, ref: Ref) -> Vertex | None:
    """El vértice `ref` del user, o `None` si no existe / el slug no es proyectable (p.ej. `inbox`,
    que no es vértice) / la fila está filtrada (consolidado borrado)."""
    src = _BY_SLUG.get(ref.slug)
    if src is None:
        return None
    row = (
        conn.execute(text(_select_for(src, by_id=True)), {"uid": user_id, "id": ref.id})
        .mappings()
        .first()
    )
    if row is None:
        return None
    return Vertex(str(row["slug"]), int(row["id"]), str(row["label"]), str(row["kind"]))


def known_slugs() -> tuple[str, ...]:
    """Los slugs de vértice proyectables (los tipos de vértice de dominio)."""
    return tuple(s.slug for s in NODE_SOURCES)
