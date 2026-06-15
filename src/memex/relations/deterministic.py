"""Tejedores de las aristas REALES del grafo: las que un dato ya guardado GARANTIZA (no
co-ocurrencia, no LLM). Cada módulo las teje INCREMENTAL al escribir (paso 5 del pipeline,
`weave_*`), en la misma tx del caller, para no depender de un barrido global. Idempotentes.

Aristas reales (nacen `extracted+confirmed`):
- afiliación (`producer='identidades'`): persona→org que el directorio enlaza explícitamente.
- pertenencia (`producer='identidades'`): sub→padre (`parent_identity_id`): programa→universidad.
- contraparte (`producer='finance'`): cobro/pago CONSOLIDADO → identidad del cobrador/pagador
  resuelto (`counterparty_identity_id`). El enlace por identidad finanzas↔directorio.
- cumple (`producer='bienestar'`): registro → hábito que cumple (match determinista de
  `activity`/`category`).
- mismo_evento (`producer='event'`): hechos que comparten `event_id` (la correlación de Hermes).
- participa_en (`producer='canal'`): persona → canal de chat donde escribió (remitente resuelto).

Cada materializador `_*` admite un scope opcional (uso incremental) o barre todo (sin scope). Los
`_*_pairs` (read-only) exponen los pares vigentes HOY: los reusa la reconciliación de
`relations.maintenance`. La generación de co-ocurrencia vive en `relations.cooccurrence`; la poda
y la reconciliación, en `relations.maintenance`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.relations.edges import (
    CANAL_SLUG,
    PRODUCER_BIENESTAR,
    PRODUCER_CANAL,
    PRODUCER_EVENT,
    PRODUCER_FINANCE,
    PRODUCER_IDENTIDADES,
    PROVENANCE_EXTRACTED,
    RELTYPE_PARTICIPA_EN,
    VERDICT_CONFIRMED,
    Ref,
    propose_edge,
)
from memex.relations.vertices import IDENTITY_SLUG_BY_KIND

#: Normalización SQL del `activity` para el match registro↔hábito (lower + colapso de whitespace).
#: Copia DELIBERADA del fragmento de `bienestar` (habits.py / module.py): NO se importa de ahí
#: porque esos módulos ya importan este paquete → invertir la dependencia crearía un ciclo.
_NORM_ACTIVITY = "lower(btrim(regexp_replace({x}, '\\s+', ' ', 'g')))"

#: CASE SQL kind→slug de identidad, generado desde el único punto de verdad (vertices.py). El ELSE
#: NULL es defensivo: un kind fuera del mapa no emite arista (y el SELECT final lo filtra).
_IDENTITY_KIND_CASE = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in IDENTITY_SLUG_BY_KIND.items())


# --- afiliación (persona→org) --------------------------------------------------------- #


def _afiliacion_pairs(
    conn: Connection, user_id: int, *, person_ids: Sequence[int] | None = None
) -> Iterator[tuple[Ref, Ref]]:
    """Los pares persona→org que el directorio enlaza explícitamente HOY (read-only). Con
    `person_ids` acota a esas personas; sin él, todas. Base del tejido y de la reconciliación."""
    scope = "" if person_ids is None else " AND person_id = ANY(:pids)"
    params: dict[str, Any] = {"u": user_id}
    if person_ids is not None:
        params["pids"] = list(person_ids)
    for r in conn.execute(
        text(
            f"SELECT person_id, org_id FROM mod_identidades_person_orgs WHERE user_id = :u{scope}"
        ),
        params,
    ).mappings():
        yield (
            Ref("identidades:person", int(r["person_id"])),
            Ref("identidades:org", int(r["org_id"])),
        )


def _materialize_afiliacion(
    conn: Connection, user_id: int, *, person_ids: Sequence[int] | None = None
) -> int:
    """Una arista REAL persona→org por cada enlace explícito del directorio. Con `person_ids` acota
    (incremental); sin él, barre todas. Idempotente. Devuelve cuántas."""
    n = 0
    for src, dst in _afiliacion_pairs(conn, user_id, person_ids=person_ids):
        propose_edge(
            conn,
            user_id,
            src,
            dst,
            producer=PRODUCER_IDENTIDADES,
            relation_type="afiliado",
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
        )
        n += 1
    return n


# --- pertenencia (sub→padre) ---------------------------------------------------------- #


def _pertenencia_pairs(
    conn: Connection, user_id: int, *, child_ids: Sequence[int] | None = None
) -> Iterator[tuple[Ref, Ref]]:
    """Los pares hijo→padre de la jerarquía «sub» del directorio HOY (read-only). Con `child_ids`
    acota a esos hijos; sin él, todos."""
    scope = "" if child_ids is None else " AND c.id = ANY(:cids)"
    params: dict[str, Any] = {"u": user_id}
    if child_ids is not None:
        params["cids"] = list(child_ids)
    for r in conn.execute(
        text(
            f"""
            SELECT c.id AS child_id, c.kind AS child_kind,
                   p.id AS parent_id, p.kind AS parent_kind
            FROM mod_identidades c
            JOIN mod_identidades p ON p.id = c.parent_identity_id
            WHERE c.user_id = :u AND c.parent_identity_id IS NOT NULL{scope}
            """
        ),
        params,
    ).mappings():
        yield (
            Ref(IDENTITY_SLUG_BY_KIND[str(r["child_kind"])], int(r["child_id"])),
            Ref(IDENTITY_SLUG_BY_KIND[str(r["parent_kind"])], int(r["parent_id"])),
        )


def _materialize_pertenencia(
    conn: Connection, user_id: int, *, child_ids: Sequence[int] | None = None
) -> int:
    """Una arista REAL «pertenece_a» sub→padre por cada `parent_identity_id` del directorio (la
    jerarquía genérica «sub»: programa→universidad, producto→empresa, …). Dirigida (hijo→padre). Con
    `child_ids` acota (incremental); sin él, barre todas. Idempotente. Devuelve cuántas."""
    n = 0
    for src, dst in _pertenencia_pairs(conn, user_id, child_ids=child_ids):
        propose_edge(
            conn,
            user_id,
            src,
            dst,
            producer=PRODUCER_IDENTIDADES,
            relation_type="pertenece_a",
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
        )
        n += 1
    return n


# --- contraparte (cobro→identidad) ---------------------------------------------------- #


def _contraparte_pairs(
    conn: Connection, user_id: int, *, consolidated_ids: Sequence[int] | None = None
) -> Iterator[tuple[Ref, Ref]]:
    """Los pares cobro→identidad de finanzas consolidadas cuya contraparte resolvió HOY (read-only).
    Con `consolidated_ids` acota; sin él, todos."""
    scope = "" if consolidated_ids is None else " AND c.id = ANY(:cids)"
    params: dict[str, Any] = {"u": user_id}
    if consolidated_ids is not None:
        params["cids"] = list(consolidated_ids)
    for r in conn.execute(
        text(
            f"""
            SELECT c.id AS cid, i.id AS iid, i.kind AS kind
            FROM mod_finance_consolidated c
            JOIN mod_identidades i ON i.id = c.counterparty_identity_id
            WHERE c.user_id = :u AND NOT c.deleted AND c.counterparty_identity_id IS NOT NULL{scope}
            """
        ),
        params,
    ).mappings():
        yield (
            Ref("finance", int(r["cid"])),
            Ref(IDENTITY_SLUG_BY_KIND[str(r["kind"])], int(r["iid"])),
        )


def _materialize_contraparte(
    conn: Connection, user_id: int, *, consolidated_ids: Sequence[int] | None = None
) -> int:
    """Una arista REAL «contraparte» cobro→identidad por cada transacción CONSOLIDADA cuya
    contraparte resolvió a una identidad del directorio (`counterparty_identity_id`). Dirigida (el
    cobro/pago → quién cobró/pagó). El enlace por identidad finanzas↔directorio. Con
    `consolidated_ids` acota (incremental); sin él, barre todos. Idempotente. Devuelve cuántas."""
    n = 0
    for src, dst in _contraparte_pairs(conn, user_id, consolidated_ids=consolidated_ids):
        propose_edge(
            conn,
            user_id,
            src,
            dst,
            producer=PRODUCER_FINANCE,
            relation_type="contraparte",
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
        )
        n += 1
    return n


# --- mismo_evento (event_id compartido) ----------------------------------------------- #


def _materialize_same_event(
    conn: Connection, user_id: int, *, event_ids: Sequence[str] | None = None
) -> int:
    """Una arista REAL «mismo_evento» entre hechos que comparten `event_id` — los que el agente
    (Hermes) correlacionó en un mismo mensaje. CROSS-MODULE: bienestar (el registro), finanzas (la
    transacción mapeada a su CONSOLIDADO, el vértice del grafo) e identidades (la mención-evento
    que crea `register_card` al cerrar el evento, resuelta a su identidad); `event_id` NULL no
    correlaciona. Par canónico por `(slug, id)`. Con `event_ids` acota a esos eventos (uso
    incremental, todos los brazos del CTE); sin él, barre todos (full-sweep). Idempotente.
    Devuelve cuántas."""
    scope_b = "" if event_ids is None else " AND event_id = ANY(:eids)"
    scope_f = "" if event_ids is None else " AND t.event_id = ANY(:eids)"
    scope_m = "" if event_ids is None else " AND m.event_id = ANY(:eids)"
    params: dict[str, Any] = {"u": user_id}
    if event_ids is not None:
        params["eids"] = list(event_ids)
    n = 0
    for r in conn.execute(
        text(
            f"""
            WITH facts AS (
                SELECT 'bienestar' AS slug, id AS vid, event_id
                FROM mod_bienestar_registros
                WHERE user_id = :u AND event_id IS NOT NULL{scope_b}
                UNION
                SELECT 'finance' AS slug, c.id AS vid, t.event_id
                FROM mod_finance_transactions t
                JOIN mod_finance_transaction_links l ON l.transaction_id = t.id
                JOIN mod_finance_consolidated c ON c.id = l.consolidated_id AND NOT c.deleted
                WHERE t.user_id = :u AND t.event_id IS NOT NULL{scope_f}
                UNION
                SELECT (CASE i.kind {_IDENTITY_KIND_CASE} ELSE NULL END) AS slug,
                       m.resolved_identity_id AS vid, m.event_id
                FROM mod_identidades_mentions m
                JOIN mod_identidades i ON i.id = m.resolved_identity_id
                WHERE m.user_id = :u AND m.event_id IS NOT NULL
                  AND m.resolved_identity_id IS NOT NULL{scope_m}
            )
            SELECT a.slug AS a_slug, a.vid AS a_vid, b.slug AS b_slug, b.vid AS b_vid
            FROM facts a
            JOIN facts b ON a.event_id = b.event_id AND (a.slug, a.vid) < (b.slug, b.vid)
            WHERE a.slug IS NOT NULL AND b.slug IS NOT NULL
            """
        ),
        params,
    ).mappings():
        propose_edge(
            conn,
            user_id,
            Ref(str(r["a_slug"]), int(r["a_vid"])),
            Ref(str(r["b_slug"]), int(r["b_vid"])),
            producer=PRODUCER_EVENT,
            relation_type="mismo_evento",
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
            evidence="event_id",
        )
        n += 1
    return n


# --- cumple (registro→hábito) --------------------------------------------------------- #


def _materialize_cumple(
    conn: Connection,
    user_id: int,
    *,
    registro_ids: Sequence[int] | None = None,
    habit_ids: Sequence[int] | None = None,
) -> int:
    """Una arista REAL «cumple» registro→hábito por cada registro de bienestar que satisface un
    hábito ACTIVO. Match determinista idéntico a la adherencia (`habits._period_counts`): si el
    hábito define `activity`, iguala la actividad normalizada (insensible a mayúsculas/espacios);
    si no, iguala la `category`. Dirigida (registro → hábito). Un registro puede cumplir varios
    hábitos. Con `registro_ids`/`habit_ids` acota a esos lados (uso incremental); sin ellos barre
    todo (full-sweep). Idempotente. Devuelve cuántas."""
    scope = ""
    params: dict[str, Any] = {"u": user_id}
    if registro_ids is not None:
        scope += " AND r.id = ANY(:rids)"
        params["rids"] = list(registro_ids)
    if habit_ids is not None:
        scope += " AND h.id = ANY(:hids)"
        params["hids"] = list(habit_ids)
    norm_r = _NORM_ACTIVITY.format(x="r.activity")
    norm_h = _NORM_ACTIVITY.format(x="h.activity")
    n = 0
    for r in conn.execute(
        text(
            f"""
            SELECT r.id AS rid, h.id AS hid
            FROM mod_bienestar_registros r
            JOIN mod_bienestar_habits h ON h.user_id = r.user_id AND h.active AND (
                (h.activity <> '' AND {norm_r} = {norm_h})
                OR (h.activity = '' AND r.category = h.category)
            )
            WHERE r.user_id = :u{scope}
            """
        ),
        params,
    ).mappings():
        propose_edge(
            conn,
            user_id,
            Ref("bienestar", int(r["rid"])),
            Ref("bienestar:habito", int(r["hid"])),
            producer=PRODUCER_BIENESTAR,
            relation_type="cumple",
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
            evidence="cumple",
        )
        n += 1
    return n


# --- participa_en (identidad→canal) --------------------------------------------------- #


def weave_participa_en(
    conn: Connection, user_id: int, inbox_ids: Sequence[int] | None = None
) -> int:
    """REAL: una arista «participa_en» identidad→canal por cada remitente RESUELTO (identifier
    `platform_id`) que escribió en ese canal. Dirigida (quién → dónde). La estructura del medio:
    sobrevive aunque el mensaje sea over-cap (no depende de la co-ocurrencia). Con `inbox_ids` acota
    a esos mensajes (tejido por-lote desde `weave_chat_structure`); sin él, barre todos. Bots fuera
    (relay ≠ persona). Idempotente. Devuelve cuántas."""
    scope = "" if inbox_ids is None else " AND i.id = ANY(:ids)"
    params: dict[str, Any] = {"u": user_id}
    if inbox_ids is not None:
        params["ids"] = list(inbox_ids)
    n = 0
    for r in conn.execute(
        text(
            f"""
            SELECT DISTINCT c.id AS canal_id, idf.identity_id AS iid, ident.kind AS kind
            FROM inbox i
            JOIN mod_canales c ON c.user_id = i.user_id AND c.platform = 'telegram'
                              AND c.external_id = i.payload->>'chat_id'
            JOIN mod_identidades_identifiers idf
              ON idf.user_id = i.user_id AND idf.platform = 'telegram'
             AND idf.kind = 'platform_id'
             AND idf.value_norm = i.payload->'sender'->>'user_id'
            JOIN mod_identidades ident ON ident.id = idf.identity_id
            WHERE i.user_id = :u AND i.payload->>'chat_id' IS NOT NULL
              AND (i.payload->'sender'->>'is_bot')::boolean IS NOT TRUE{scope}
            """
        ),
        params,
    ).mappings():
        slug = IDENTITY_SLUG_BY_KIND[str(r["kind"])]
        propose_edge(
            conn,
            user_id,
            Ref(slug, int(r["iid"])),
            Ref(CANAL_SLUG, int(r["canal_id"])),
            producer=PRODUCER_CANAL,
            relation_type=RELTYPE_PARTICIPA_EN,
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
            evidence="sender",
        )
        n += 1
    return n


# --- weaves incrementales (paso 5: el módulo teje al escribir) ------------------------- #


def weave_event(conn: Connection, user_id: int, event_id: str) -> int:
    """INCREMENTAL: teje las aristas «mismo_evento» de UN evento, en la misma tx del caller. Lo
    llaman los módulos al escribir un hecho con `event_id`, para no depender del full-sweep. Si el
    otro extremo aún no existe, no crea nada todavía (lo creará quien aterrice último). Idempotente.
    Devuelve cuántas aristas tocó."""
    if not event_id:
        return 0
    return _materialize_same_event(conn, user_id, event_ids=[event_id])


def weave_afiliacion(conn: Connection, user_id: int, person_id: int) -> int:
    """INCREMENTAL: teje la arista «afiliado» de las afiliaciones de UNA persona recién enlazada, en
    la misma tx del caller. Lo llama `identidades.register_card` al crear la afiliación, para no
    depender del full-sweep. Ambos extremos (persona/org) ya existen. Idempotente. Devuelve
    cuántas."""
    return _materialize_afiliacion(conn, user_id, person_ids=[person_id])


def weave_pertenencia(conn: Connection, user_id: int, child_id: int) -> int:
    """INCREMENTAL: teje la arista «pertenece_a» de UN hijo recién apuntado a su padre
    (`parent_identity_id`), en la misma tx del caller. Lo llaman los puntos que asignan el padre
    (API/CLI `set-parent`, organizador LLM, merge), para no depender del full-sweep. Si el hijo no
    tiene padre, no crea nada. Idempotente. Devuelve cuántas."""
    return _materialize_pertenencia(conn, user_id, child_ids=[child_id])


def weave_finance_consolidated(
    conn: Connection,
    user_id: int,
    consolidated_ids: Sequence[int],
    event_ids: Sequence[str],
) -> tuple[int, int]:
    """INCREMENTAL: tras consolidar finanzas (donde nace su vértice), teje «contraparte» de esos
    consolidados y «mismo_evento» de sus eventos, en la misma tx. Idempotente. Devuelve
    (contraparte, mismo_evento)."""
    cids = list(consolidated_ids)
    eids = [e for e in event_ids if e]
    contraparte = _materialize_contraparte(conn, user_id, consolidated_ids=cids) if cids else 0
    same_event = _materialize_same_event(conn, user_id, event_ids=eids) if eids else 0
    return contraparte, same_event


def weave_cumple(
    conn: Connection,
    user_id: int,
    *,
    registro_ids: Sequence[int] | None = None,
    habit_ids: Sequence[int] | None = None,
) -> int:
    """INCREMENTAL: teje las aristas «cumple» de UN registro recién creado (`registro_ids`) o de UN
    hábito recién creado (`habit_ids`), en la misma tx del caller, para no depender del full-sweep.
    Lo llaman `bienestar.register` (lado registro) y `bienestar.add_habit` (lado hábito). Si el otro
    lado aún no existe, no crea nada todavía (lo creará quien aterrice último). Idempotente.
    Devuelve cuántas."""
    return _materialize_cumple(conn, user_id, registro_ids=registro_ids, habit_ids=habit_ids)
