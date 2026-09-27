"""Cronología (timeline / story) de un cúmulo: sus miembros FECHADOS como SUCESOS ordenados en
el tiempo + el ELENCO (miembros sin fecha de evento: identidades, hábitos). Solo lectura.

Reusa la proyección de vértices (`list_vertices`, etiqueta/tipo de `NODE_SOURCES`) y la procedencia
(`vertex_inbox_ids`, drill-down al correo por suceso). Cada tipo fechado saca su fecha de su tabla
(`_DATE_SOURCES`): finance/bienestar = `occurred_at` TIMESTAMPTZ con precisión (datetime/date/
inferred); calendar = `starts_on` DATE + `start_time` TIME (nullable); hackathones = `starts_on`
DATE (puede ser NULL → al elenco).

TZ: todo se normaliza a hora LOCAL (America/Bogota, ver `user-timezone-bogota`) para que el front
solo formatee; `precision` permite mostrar la hora solo cuando es real (`inferred` → marca "≈").
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.relations.cooccurrence import vertex_inbox_ids
from memex.relations.edges import Ref
from memex.relations.vertices import list_vertices

#: TZ de display. Fijo a America/Bogotá (UTC-5, sin DST) — la del usuario. Configurable a futuro.
_TZ = ZoneInfo("America/Bogota")


@dataclass(frozen=True)
class _DateSource:
    """Cómo sacar `(id, at, prec)` de la tabla de un tipo de vértice fechado."""

    table: str
    select: str  # produce columnas `id`, `at` (timestamp/date), `prec` (text)
    where: str  # filtro extra (p.ej. NOT deleted), o ""


#: slug → su fuente de fecha. Literales internos (no input de usuario): seguro interpolar.
_DATE_SOURCES: dict[str, _DateSource] = {
    "finance": _DateSource(
        "mod_finance_consolidated",
        "id, occurred_at AS at, occurred_at_precision AS prec",
        "NOT deleted",
    ),
    "bienestar": _DateSource(
        "mod_bienestar_registros",
        "id, occurred_at AS at, occurred_at_precision AS prec",
        "",
    ),
    "calendar": _DateSource(
        "mod_calendar_consolidated",
        "id, (starts_on + COALESCE(start_time, '00:00:00'::time)) AS at, "
        "CASE WHEN start_time IS NULL THEN 'date' ELSE 'datetime' END AS prec",
        "NOT deleted",
    ),
    "hackathones": _DateSource(
        "mod_hackathones_events",
        "id, starts_on AS at, 'date' AS prec",
        "",
    ),
}


@dataclass(frozen=True)
class TimelineEvent:
    """Un suceso fechado del cúmulo (un miembro con fecha)."""

    slug: str
    id: int
    kind: str
    label: str
    at: str  # ISO en hora local (fecha sola si `precision != 'datetime'`)
    precision: str  # 'datetime' | 'date' | 'inferred'
    source_inbox_ids: list[int]


@dataclass(frozen=True)
class TimelineActor:
    """Un miembro SIN fecha de evento (elenco/contexto: identidad, hábito, o fecha NULL)."""

    slug: str
    id: int
    kind: str
    label: str
    source_inbox_ids: list[int]


@dataclass(frozen=True)
class ClusterMeta:
    """Cabecera del cúmulo (título + sinopsis de la story)."""

    id: int
    name: str
    description: str
    confidence: float | None
    member_count: int


@dataclass(frozen=True)
class ClusterTimeline:
    """La cronología completa: cabecera + sucesos ordenados + elenco."""

    cluster: ClusterMeta
    events: list[TimelineEvent]
    actors: list[TimelineActor]


def _normalize(raw: object, prec: str) -> tuple[str, str, datetime] | None:
    """`(iso_local, precision, sort_key)` o `None` si no hay fecha. `sort_key` = datetime local."""
    if isinstance(raw, datetime):
        local = raw.astimezone(_TZ).replace(tzinfo=None) if raw.tzinfo is not None else raw
        if prec == "datetime":
            iso = local.replace(microsecond=0).isoformat()
        else:
            iso = local.date().isoformat()
        return (iso, prec, datetime.combine(local.date(), local.time()))
    if isinstance(raw, date):  # hackathones starts_on (DATE puro)
        return (raw.isoformat(), "date", datetime.combine(raw, time.min))
    return None


def cluster_timeline(conn: Connection, user_id: int, cluster_id: int) -> ClusterTimeline | None:
    """Cronología de un cúmulo CONFIRMADO del user. `None` si no existe o no es confirmed."""
    row = (
        conn.execute(
            text(
                "SELECT id, name, description, confidence, member_count FROM relation_clusters "
                "WHERE id = :c AND user_id = :u AND status = 'confirmed'"
            ),
            {"c": cluster_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    meta = ClusterMeta(
        int(row["id"]),
        str(row["name"]),
        str(row["description"]),
        float(row["confidence"]) if row["confidence"] is not None else None,
        int(row["member_count"]),
    )

    members = [
        Ref(str(m["member_slug"]), int(m["member_id"]))
        for m in conn.execute(
            text(
                "SELECT member_slug, member_id FROM relation_cluster_members "
                "WHERE cluster_id = :c AND NOT pruned"
            ),
            {"c": cluster_id},
        ).mappings()
    ]
    vmap = {v.ref: v for v in list_vertices(conn, user_id)}
    prov = vertex_inbox_ids(conn, user_id)
    by_slug: dict[str, list[int]] = defaultdict(list)
    for ref in members:
        by_slug[ref.slug].append(ref.id)

    dated: list[tuple[datetime, TimelineEvent]] = []
    actors: list[TimelineActor] = []
    for slug, ids in by_slug.items():
        src = _DATE_SOURCES.get(slug)
        if src is None:  # identidades / hábito → elenco
            for i in ids:
                v = vmap.get(Ref(slug, i))
                if v is not None:
                    actors.append(
                        TimelineActor(slug, i, v.kind, v.label, sorted(prov.get(v.ref, set())))
                    )
            continue
        where = f" AND {src.where}" if src.where else ""
        rows = conn.execute(
            text(
                f"SELECT {src.select} FROM {src.table} WHERE user_id = :u AND id = ANY(:ids){where}"
            ),
            {"u": user_id, "ids": ids},
        ).mappings()
        for row in rows:
            i = int(row["id"])
            v = vmap.get(Ref(slug, i))
            if v is None:
                continue
            inbox = sorted(prov.get(v.ref, set()))
            norm = _normalize(row["at"], str(row["prec"]))
            if norm is None:  # tipo fechado pero fecha NULL (p.ej. hackatón sin fecha) → elenco
                actors.append(TimelineActor(slug, i, v.kind, v.label, inbox))
                continue
            iso, precision, sort_key = norm
            dated.append((sort_key, TimelineEvent(slug, i, v.kind, v.label, iso, precision, inbox)))

    dated.sort(key=lambda t: t[0])
    return ClusterTimeline(meta, [e for _, e in dated], actors)
