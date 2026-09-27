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

from memex.relations.decisions import add_edge_sources
from memex.relations.edges import (
    ASUNTO_SLUG,
    CALENDAR_SLUG,
    CANAL_SLUG,
    PRODUCER_ASUNTOS,
    PRODUCER_BIENESTAR,
    PRODUCER_CALENDAR,
    PRODUCER_CANAL,
    PRODUCER_EVENT,
    PRODUCER_FINANCE,
    PRODUCER_IDENTIDADES,
    PRODUCER_TAREAS,
    PROVENANCE_EXTRACTED,
    RELTYPE_ASISTE,
    RELTYPE_INVOLUCRA,
    RELTYPE_ORGANIZA,
    RELTYPE_PARTE_DE,
    RELTYPE_PARTICIPA_EN,
    TAREA_SLUG,
    VERDICT_CONFIRMED,
    Ref,
    list_edges,
)
from memex.relations.graph_writer import add_edge
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
    """Los pares «afiliado»→org que el directorio enlaza explícitamente HOY (read-only). El lado
    afiliado suele ser una PERSONA, pero también puede ser un `desconocido`: un remitente de dominio
    corporativo cuyo tipo aún no se definió queda afiliado a la org del dominio («pertenece a este
    dominio» vale aunque el tipo sea incierto). Por eso el slug del origen sale del kind REAL (no se
    asume persona) — si no, una afiliación de un desconocido proyectaría un vértice inexistente que
    la poda de huérfanas barrería. Con `person_ids` acota a esos afiliados; sin él, todas. Base del
    tejido y de la reconciliación."""
    scope = "" if person_ids is None else " AND po.person_id = ANY(:pids)"
    params: dict[str, Any] = {"u": user_id}
    if person_ids is not None:
        params["pids"] = list(person_ids)
    for r in conn.execute(
        text(
            f"""
            SELECT po.person_id AS person_id, po.org_id AS org_id, p.kind AS person_kind
            FROM mod_identidades_person_orgs po
            JOIN mod_identidades p ON p.id = po.person_id AND p.user_id = po.user_id
            WHERE po.user_id = :u{scope}
            """
        ),
        params,
    ).mappings():
        yield (
            Ref(IDENTITY_SLUG_BY_KIND[str(r["person_kind"])], int(r["person_id"])),
            Ref("identidades:org", int(r["org_id"])),
        )


def _materialize_afiliacion(
    conn: Connection, user_id: int, *, person_ids: Sequence[int] | None = None
) -> int:
    """Una arista REAL persona→org por cada enlace explícito del directorio. Con `person_ids` acota
    (incremental); sin él, barre todas. Idempotente. Devuelve cuántas."""
    n = 0
    for src, dst in _afiliacion_pairs(conn, user_id, person_ids=person_ids):
        add_edge(
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
        add_edge(
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
        add_edge(
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


# --- calendar: organiza/asiste (evento→identidad) ------------------------------------- #


def _calendar_asiste_includes_declined(conn: Connection, user_id: int) -> bool:
    """Lee la perilla `asiste_includes_declined` de `module_settings.config` (módulo calendar):
    ¿un invitado que RECHAZÓ cuenta como que asiste? Ausente ⇒ False (no cuenta). La clave canónica
    y su writer viven en `calendar.settings`; acá se lee con SQL inline (NO se importa: `calendar`
    ya importa `relations` → invertir crearía ciclo, misma convención que `_NORM_ACTIVITY`)."""
    return bool(
        conn.execute(
            text(
                "SELECT (config->>'asiste_includes_declined') = 'true' "
                "FROM module_settings WHERE user_id = :u AND module_slug = 'calendar'"
            ),
            {"u": user_id},
        ).scalar()
    )


def _calendar_participa_pairs(
    conn: Connection, user_id: int, *, consolidated_ids: Sequence[int] | None = None
) -> Iterator[tuple[Ref, Ref, str]]:
    """Pares identidad→evento de calendario vigentes HOY (read-only), con su `relation_type`
    («organiza» para el organizador, «asiste» para un asistente). Resuelve al participante por EMAIL
    con un join exacto sobre `value_norm` (lo pobló el MISMO `norm_identifier` que `email_norm`).
    Filtra self/recursos y veta `producto`; los `declined` entran solo si la perilla lo pide (el
    organizador siempre, sin importar su respuesta). Con `consolidated_ids` acota; sin él, barre
    todos. Fuente de verdad ÚNICA del weave y de la reconciliación.

    Dirección evento→identidad (el tipo va en `relation_type`): igual que el hermano `contraparte`.
    """
    scope = "" if consolidated_ids is None else " AND c.id = ANY(:cids)"
    params: dict[str, Any] = {
        "u": user_id,
        "incl": _calendar_asiste_includes_declined(conn, user_id),
    }
    if consolidated_ids is not None:
        params["cids"] = list(consolidated_ids)
    for r in conn.execute(
        text(
            f"""
            SELECT DISTINCT c.id AS cid, i.id AS iid, i.kind AS kind, p.role AS role
            FROM mod_calendar_consolidated c
            JOIN mod_calendar_event_links l ON l.consolidated_id = c.id
            JOIN mod_calendar_event_participants p ON p.event_id = l.event_id
            JOIN mod_identidades_identifiers idf
              ON idf.user_id = c.user_id AND idf.kind = 'email' AND idf.value_norm = p.email_norm
            JOIN mod_identidades i ON i.id = idf.identity_id AND i.user_id = c.user_id
            WHERE c.user_id = :u AND NOT c.deleted
              AND p.email_norm <> ''
              AND NOT p.is_self AND NOT p.is_resource
              AND i.kind IN ('persona', 'organizacion', 'desconocido')
              AND (p.role = 'organizer'
                   OR CAST(:incl AS BOOLEAN)
                   OR p.response_status IS DISTINCT FROM 'declined'){scope}
            """
        ),
        params,
    ).mappings():
        reltype = RELTYPE_ORGANIZA if str(r["role"]) == "organizer" else RELTYPE_ASISTE
        yield (
            Ref(CALENDAR_SLUG, int(r["cid"])),
            Ref(IDENTITY_SLUG_BY_KIND[str(r["kind"])], int(r["iid"])),
            reltype,
        )


def weave_calendar_consolidated(
    conn: Connection, user_id: int, consolidated_ids: Sequence[int] | None = None
) -> int:
    """REAL: aristas «organiza»/«asiste» evento→identidad por cada participante RESUELTO por email.
    Lo llama el finisher de la consolidación de calendar (donde nace el vértice del evento), en la
    misma tx. FULL-SWEEP por defecto (capta cambios de participantes que NO reescriben el
    consolidado e identidades que resuelven tarde — el join es lazy). Diff `desired - existing` para
    NO re-marcar dirty lo ya tejido (ni resucitar una arista que un humano rechazó). Idempotente
    (además `add_edge` colapsa por el unique lógico). Devuelve cuántas aristas creó."""
    desired = set(_calendar_participa_pairs(conn, user_id, consolidated_ids=consolidated_ids))
    existing = {
        (e.src, e.dst, e.relation_type)
        for e in list_edges(conn, user_id, producer=PRODUCER_CALENDAR)
    }
    n = 0
    for src, dst, reltype in desired - existing:
        add_edge(
            conn,
            user_id,
            src,
            dst,
            producer=PRODUCER_CALENDAR,
            relation_type=reltype,
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
            evidence="organizer" if reltype == RELTYPE_ORGANIZA else "attendee",
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
    scope_t = "" if event_ids is None else " AND event_id = ANY(:eids)"
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
                UNION
                SELECT 'tarea' AS slug, id AS vid, event_id
                FROM mod_tareas
                WHERE user_id = :u AND event_id IS NOT NULL AND status <> 'cancelada'{scope_t}
            )
            SELECT a.slug AS a_slug, a.vid AS a_vid, b.slug AS b_slug, b.vid AS b_vid
            FROM facts a
            JOIN facts b ON a.event_id = b.event_id AND (a.slug, a.vid) < (b.slug, b.vid)
            WHERE a.slug IS NOT NULL AND b.slug IS NOT NULL
            """
        ),
        params,
    ).mappings():
        add_edge(
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
        add_edge(
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
            FROM inbox i JOIN sources s ON s.id = i.source_id
            JOIN mod_canales c ON c.user_id = i.user_id AND c.platform = s.type
                              AND c.external_id = i.payload->>'chat_id'
            JOIN mod_identidades_identifiers idf
              ON idf.user_id = i.user_id AND idf.platform = s.type
             AND idf.kind = 'platform_id'
             AND idf.value_norm = COALESCE(i.payload->'sender'->>'user_id',
                                           i.payload->'sender'->>'phone')
            JOIN mod_identidades ident ON ident.id = idf.identity_id
            WHERE i.user_id = :u AND i.payload->>'chat_id' IS NOT NULL
              AND (i.payload->'sender'->>'is_bot')::boolean IS NOT TRUE{scope}
            """
        ),
        params,
    ).mappings():
        slug = IDENTITY_SLUG_BY_KIND[str(r["kind"])]
        add_edge(
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


# --- asuntos → elenco / asuntos → asuntos ------------------------------------------------- #


def _asuntos_pairs(
    conn: Connection, user_id: int, *, asunto_ids: Sequence[int] | None = None
) -> Iterator[tuple[Ref, Ref, str, frozenset[int]]]:
    """Pares vigentes HOY (read-only) del productor `asuntos`, con tipo y mensajes compartidos:
    - `asunto → vértice` «involucra» por cada vértice (identidad, evento, transacción, canal…) cuya
      procedencia comparte al menos un mensaje con la evidencia ACTIVA del asunto;
    - `asunto → asunto` con el tipo de `mod_asuntos_relations` (procedencia vacía).
    Solo asuntos con vértice (no candidatos, no fundidos). Fuente de verdad ÚNICA del weave y de
    la reconciliación."""
    from memex.relations.cooccurrence import vertex_inbox_ids

    scope = "" if asunto_ids is None else " AND a.id = ANY(:aids)"
    params: dict[str, Any] = {"u": user_id}
    if asunto_ids is not None:
        params["aids"] = list(asunto_ids)
    evidence: dict[int, set[int]] = {}
    for r in conn.execute(
        text(
            f"""
            SELECT a.id AS aid, e.inbox_id AS iid
            FROM mod_asuntos a
            JOIN mod_asuntos_evidence e ON e.asunto_id = a.id AND e.status = 'active'
            WHERE a.user_id = :u AND a.merged_into_id IS NULL
              AND a.status IN ('abierto','cerrado','inactivo'){scope}
            """
        ),
        params,
    ):
        evidence.setdefault(int(r[0]), set()).add(int(r[1]))
    if evidence:
        by_inbox: dict[int, set[Ref]] = {}
        for ref, mids in vertex_inbox_ids(conn, user_id).items():
            if ref.slug == ASUNTO_SLUG:
                continue
            for mid in mids:
                by_inbox.setdefault(mid, set()).add(ref)
        for aid, mids in evidence.items():
            shared: dict[Ref, set[int]] = {}
            for mid in mids:
                for ref in by_inbox.get(mid, ()):
                    shared.setdefault(ref, set()).add(mid)
            for ref, common in shared.items():
                yield Ref(ASUNTO_SLUG, aid), ref, RELTYPE_INVOLUCRA, frozenset(common)
    rel_scope = (
        ""
        if asunto_ids is None
        else " AND (r.src_asunto_id = ANY(:aids) OR r.dst_asunto_id = ANY(:aids))"
    )
    for r in conn.execute(
        text(
            f"""
            SELECT r.src_asunto_id, r.dst_asunto_id, r.relation_type
            FROM mod_asuntos_relations r
            JOIN mod_asuntos s ON s.id = r.src_asunto_id
            JOIN mod_asuntos d ON d.id = r.dst_asunto_id
            WHERE r.user_id = :u AND r.status = 'active'
              AND s.merged_into_id IS NULL AND d.merged_into_id IS NULL
              AND s.status IN ('abierto','cerrado','inactivo')
              AND d.status IN ('abierto','cerrado','inactivo'){rel_scope}
            """
        ),
        params,
    ):
        yield Ref(ASUNTO_SLUG, int(r[0])), Ref(ASUNTO_SLUG, int(r[1])), str(r[2]), frozenset()


def asuntos_live_pairs(conn: Connection, user_id: int) -> set[tuple[Ref, Ref, str]]:
    """(src, dst, tipo) vigentes del productor `asuntos` — para la reconciliación (poda)."""
    return {(s, d, t) for s, d, t, _ in _asuntos_pairs(conn, user_id)}


def weave_asuntos(conn: Connection, user_id: int, asunto_ids: Sequence[int] | None = None) -> int:
    """REAL: aristas «involucra» asunto→vértice y asunto→asunto tipadas, para los asuntos dados
    (incremental: lo llama el asignador con los asuntos tocados) o todos. Diff `desired - existing`
    para no re-marcar dirty ni resucitar rechazos; la procedencia (mensajes compartidos) se acumula
    también sobre aristas ya existentes. Devuelve cuántas aristas creó."""
    desired = {
        (s, d, t): mids for s, d, t, mids in _asuntos_pairs(conn, user_id, asunto_ids=asunto_ids)
    }
    existing = {
        (e.src, e.dst, e.relation_type): e.id
        for e in list_edges(conn, user_id, producer=PRODUCER_ASUNTOS)
    }
    n = 0
    for key, mids in desired.items():
        eid = existing.get(key)
        if eid is None:
            src, dst, reltype = key
            eid = add_edge(
                conn,
                user_id,
                src,
                dst,
                producer=PRODUCER_ASUNTOS,
                relation_type=reltype,
                verdict=VERDICT_CONFIRMED,
                provenance=PROVENANCE_EXTRACTED,
                evidence=f"mensajes={len(mids)}" if mids else "relacion",
            )
            n += 1
        if mids:
            add_edge_sources(conn, eid, sorted(mids))
    return n


def weave_all_reales(conn: Connection, user_id: int) -> dict[str, int]:
    """REPARACIÓN bajo demanda (CLI `memex-graph weave`): re-teje TODAS las aristas reales que el
    dato de origen implica HOY — afiliado, pertenece_a, contraparte, organiza/asiste — con los
    mismos tejedores idempotentes que usan los módulos al escribir. NO es una fase del pipeline
    (los módulos tejen al escribir, paso 5; el lote solo poda) sino la herramienta para cuando un
    escritor NO tejió (el caso real del 2026-09-05: 53 afiliaciones del resolvedor sin arista) o
    tras un reset parcial. Devuelve cuántas aristas tocó por relación (cuenta idempotente: incluye
    las que ya existían)."""
    return {
        "afiliado": _materialize_afiliacion(conn, user_id),
        "pertenece_a": _materialize_pertenencia(conn, user_id),
        "contraparte": _materialize_contraparte(conn, user_id),
        "calendar": weave_calendar_consolidated(conn, user_id),
        "asuntos": weave_asuntos(conn, user_id),
        "tareas": _materialize_tarea_asunto(conn, user_id),
    }


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


# --- parte_de (tarea→asunto) ---------------------------------------------------------- #


def _materialize_tarea_asunto(
    conn: Connection, user_id: int, *, tarea_ids: Sequence[int] | None = None
) -> int:
    """Una arista REAL «parte_de» tarea→asunto por cada tarea NO cancelada con `asunto_id` a un
    asunto vivo (no fundido). Con `tarea_ids` acota (uso incremental desde `tareas.add`); sin él
    barre todo (reparación). Idempotente. Devuelve cuántas."""
    scope = ""
    params: dict[str, Any] = {"u": user_id}
    if tarea_ids is not None:
        scope = " AND t.id = ANY(:tids)"
        params["tids"] = list(tarea_ids)
    n = 0
    for r in conn.execute(
        text(
            f"""
            SELECT t.id AS tid, t.asunto_id AS aid
            FROM mod_tareas t
            JOIN mod_asuntos a ON a.id = t.asunto_id AND a.merged_into_id IS NULL
            WHERE t.user_id = :u AND t.status <> 'cancelada' AND t.asunto_id IS NOT NULL{scope}
            """
        ),
        params,
    ).mappings():
        add_edge(
            conn,
            user_id,
            Ref(TAREA_SLUG, int(r["tid"])),
            Ref(ASUNTO_SLUG, int(r["aid"])),
            producer=PRODUCER_TAREAS,
            relation_type=RELTYPE_PARTE_DE,
            verdict=VERDICT_CONFIRMED,
            provenance=PROVENANCE_EXTRACTED,
            evidence="asunto_id",
        )
        n += 1
    return n


def weave_tarea(conn: Connection, user_id: int, tarea_id: int) -> int:
    """INCREMENTAL: teje la arista «parte_de» de UNA tarea recién creada o re-ligada, en la misma
    tx del caller (`tareas.add` / `tareas.update`). Idempotente. Devuelve cuántas."""
    return _materialize_tarea_asunto(conn, user_id, tarea_ids=[tarea_id])
