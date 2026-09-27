"""Almacén de asuntos: entidad, libro de evidencia, estados, relaciones, llaves y marca «visto».

Política de escritura: TODO cambio de un asunto pasa por acá (el asignador, el curador, la API y
el CLI son clientes). El vértice `asunto` del grafo se crea/funde/borra SOLO vía
`memex.relations.graph_writer` (los candidatos no tienen vértice: entran al grafo al promoverse).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.relations.edges import Ref
from memex.relations.graph_writer import add_vertex, delete_vertex, mark_dirty, merge_vertices
from memex.relevance.verdicts import EMAIL_TYPES

_log = get_logger("memex.modules.asuntos.store")

ASUNTO_SLUG = "asunto"
KINDS: frozenset[str] = frozenset(
    {"evento", "programa", "hackathon", "tramite", "proyecto", "comunidad", "servicio", "otro"}
)
STATUSES: frozenset[str] = frozenset({"candidato", "abierto", "cerrado", "inactivo"})
RELATION_TYPES: frozenset[str] = frozenset({"parte_de", "requisito_de", "continua"})
EVIDENCE_METHODS: frozenset[str] = frozenset(
    {"llm", "hilo", "evento", "asunto", "manual", "agent", "curator"}
)
#: Semillas que promueven un asunto a `abierto` con una sola evidencia (hay un compromiso real).
#: «invitacion» SALIÓ (2026-09-09): una invitación masiva abría el asunto con UN correo y el
#: dueño lo rechazó («nunca me registré, ni asistí, ni pedí más información»).
STRONG_SEEDS: frozenset[str] = frozenset({"registro", "seleccion", "conversacion"})
#: Prioridad del seguimiento (vocabulario Outlook/Jira). `alta` = plazo, plata o derecho en
#: juego, requisito de algo mayor, participación activa con entregas; `normal` = hilo vivo sin
#: urgencia; `baja` = seguimiento pasivo (comunidad, suscripción sin problema).
PRIORITIES: tuple[str, ...] = ("alta", "normal", "baja")
#: Quién fijó la prioridad; la del usuario no la pisa nadie.
PRIORITY_SOURCES: frozenset[str] = frozenset({"llm", "rule", "user", "curator"})
#: Tipos que terminan en una FECHA: si su último hito ya pasó y no hubo señal después, se
#: cierran solos (regla del sistema `close_expired`).
DATED_KINDS: frozenset[str] = frozenset({"evento", "hackathon"})
#: Estados con vértice en el grafo (los candidatos no entran hasta promoverse).
GRAPH_STATUSES: frozenset[str] = frozenset({"abierto", "cerrado", "inactivo"})

_COLS = (
    "id, user_id, name, aliases, kind, description, status, state_text, state_updated_at, seed, "
    "entities, opened_by_inbox_id, first_signal_at, last_signal_at, evidence_count, closed_at, "
    "merged_into_id, created_at, updated_at, priority, priority_reason, priority_source, due_on, "
    "(metadata->>'manual_status_at')::timestamptz AS manual_status_at, "
    "(metadata->>'silenced_at')::timestamptz AS silenced_at"
)


@dataclass(frozen=True)
class Asunto:
    id: int
    name: str
    aliases: tuple[str, ...]
    kind: str
    description: str
    status: str
    state_text: str
    state_updated_at: datetime | None
    seed: str
    entities: tuple[str, ...]
    opened_by_inbox_id: int | None
    first_signal_at: datetime | None
    last_signal_at: datetime | None
    evidence_count: int
    closed_at: datetime | None
    merged_into_id: int | None
    created_at: datetime
    updated_at: datetime
    priority: str = "normal"
    priority_reason: str = ""
    priority_source: str | None = None
    #: Última vez que el USUARIO (manual/agente) fijó el estado: hasta que llegue una señal más
    #: nueva, la regla de inactividad no lo deshace ni el curador lo cuestiona.
    manual_status_at: datetime | None = None
    #: Fecha límite del PRÓXIMO paso (plazo para actuar, fin del programa, fecha del evento). Pasada
    #: sin señal nueva → el sistema cierra el asunto (`close_expired`).
    due_on: date | None = None
    #: Silenciado por el dueño: queda cerrado aunque siga llegando señal. Nadie más lo reabre y
    #: NUNCA sale del catálogo del asignador (si saliera, el aviso siguiente abriría un duplicado).
    silenced_at: datetime | None = None

    @property
    def silenced(self) -> bool:
        return self.silenced_at is not None

    @property
    def ref(self) -> Ref:
        return Ref(ASUNTO_SLUG, self.id)


@dataclass(frozen=True)
class Evidence:
    id: int
    asunto_id: int
    inbox_id: int
    method: str
    confidence: float
    hito_date: date | None
    hito_text: str | None
    status: str
    occurred_at: datetime | None
    subject: str
    sender: str
    created_at: datetime
    #: Documento adjunto (medio `documentos`): nombre real del archivo, id del blob en
    #: `media_assets` (para bajarlo con GET /media/{id}) y tamaño. None para mensajes.
    filename: str | None = None
    media_id: int | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class StateRow:
    id: int
    state_text: str
    closed: bool
    source: str
    inbox_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class RelationRow:
    id: int
    src_asunto_id: int
    dst_asunto_id: int
    relation_type: str
    confidence: float
    source: str
    other_id: int
    other_name: str
    other_status: str
    direction: str  # "out" (este asunto es src) | "in" (este asunto es dst)


def _row_to_asunto(r: Any) -> Asunto:
    return Asunto(
        id=int(r["id"]),
        name=str(r["name"]),
        aliases=tuple(r["aliases"] or ()),
        kind=str(r["kind"]),
        description=str(r["description"] or ""),
        status=str(r["status"]),
        state_text=str(r["state_text"] or ""),
        state_updated_at=r["state_updated_at"],
        seed=str(r["seed"] or "otro"),
        entities=tuple(r["entities"] or ()),
        opened_by_inbox_id=int(r["opened_by_inbox_id"]) if r["opened_by_inbox_id"] else None,
        first_signal_at=r["first_signal_at"],
        last_signal_at=r["last_signal_at"],
        evidence_count=int(r["evidence_count"] or 0),
        closed_at=r["closed_at"],
        merged_into_id=int(r["merged_into_id"]) if r["merged_into_id"] else None,
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        priority=str(r["priority"] or "normal"),
        priority_reason=str(r["priority_reason"] or ""),
        priority_source=r["priority_source"],
        manual_status_at=r["manual_status_at"],
        silenced_at=r["silenced_at"],
        due_on=r["due_on"],
    )


# --- lectura ------------------------------------------------------------------------------ #


def get_asunto(conn: Connection, user_id: int, asunto_id: int) -> Asunto | None:
    r = (
        conn.execute(
            text(f"SELECT {_COLS} FROM mod_asuntos WHERE user_id = :u AND id = :id"),
            {"u": user_id, "id": asunto_id},
        )
        .mappings()
        .first()
    )
    return _row_to_asunto(r) if r else None


def resolve_merged(conn: Connection, user_id: int, asunto_id: int) -> int:
    """Sigue la cadena `merged_into_id` hasta el superviviente (tope 10 saltos)."""
    cur = asunto_id
    for _ in range(10):
        nxt = conn.execute(
            text("SELECT merged_into_id FROM mod_asuntos WHERE user_id = :u AND id = :id"),
            {"u": user_id, "id": cur},
        ).scalar()
        if nxt is None:
            return cur
        cur = int(nxt)
    return cur


def list_asuntos(
    conn: Connection,
    user_id: int,
    *,
    statuses: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    priorities: Sequence[str] | None = None,
    q: str | None = None,
    include_merged: bool = False,
    limit: int = 500,
    offset: int = 0,
) -> list[Asunto]:
    where = ["user_id = :u"]
    params: dict[str, Any] = {"u": user_id, "lim": limit, "off": offset}
    if not include_merged:
        where.append("merged_into_id IS NULL")
    if statuses:
        where.append("status = ANY(:st)")
        params["st"] = list(statuses)
    if kinds:
        where.append("kind = ANY(:kd)")
        params["kd"] = list(kinds)
    if priorities:
        where.append("priority = ANY(:pr)")
        params["pr"] = list(priorities)
    if q:
        where.append(
            "(name ILIKE :q OR description ILIKE :q OR state_text ILIKE :q "
            "OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE a ILIKE :q) "
            "OR EXISTS (SELECT 1 FROM unnest(entities) e WHERE e ILIKE :q))"
        )
        params["q"] = f"%{q.strip()}%"
    rows = (
        conn.execute(
            text(
                f"SELECT {_COLS} FROM mod_asuntos WHERE {' AND '.join(where)} "
                "ORDER BY last_signal_at DESC NULLS LAST, id DESC LIMIT :lim OFFSET :off"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [_row_to_asunto(r) for r in rows]


def catalog(
    conn: Connection,
    user_id: int,
    *,
    closed_window_days: int,
    around: tuple[datetime, datetime] | None = None,
) -> list[Asunto]:
    """Catálogo que ve el asignador: candidatos + abiertos + cerrados/inactivos CONTEMPORÁNEOS.

    `around` = (mínimo, máximo) de `occurred_at` de los mensajes del lote: un asunto cerrado o
    inactivo entra si su actividad (`first_signal_at`..`last_signal_at`) se solapa con ese rango
    ± `closed_window_days`. Antes la ventana se medía contra HOY, así que al procesar mensajes
    viejos (histórico, reproceso) un asunto contemporáneo pero cerrado hace meses quedaba fuera y
    el asignador abría un duplicado. Sin `around` (callers sin lote) se conserva la ventana
    contra ahora.

    El asunto SILENCIADO entra siempre, sin ventana: justo porque no se reabre, tiene que seguir
    visible para que el aviso número veinte se adhiera a él en vez de abrir un asunto nuevo del
    mismo tema."""
    if around is None:
        cond = (
            "COALESCE(closed_at, last_signal_at, updated_at) "
            ">= NOW() - make_interval(days => :days)"
        )
        params: dict[str, Any] = {"u": user_id, "days": closed_window_days}
    else:
        lo, hi = around
        cond = (
            "COALESCE(last_signal_at, closed_at, updated_at) >= :lo - make_interval(days => :days) "
            "AND COALESCE(first_signal_at, created_at) <= :hi + make_interval(days => :days)"
        )
        params = {"u": user_id, "days": closed_window_days, "lo": lo, "hi": hi}
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS} FROM mod_asuntos
                WHERE user_id = :u AND merged_into_id IS NULL
                  AND (status IN ('candidato','abierto')
                       OR metadata->>'silenced_at' IS NOT NULL
                       OR ({cond}))
                ORDER BY last_signal_at DESC NULLS LAST, id
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [_row_to_asunto(r) for r in rows]


def evidence_of(
    conn: Connection, user_id: int, asunto_id: int, *, include_rejected: bool = False
) -> list[Evidence]:
    rows = (
        conn.execute(
            text(
                """
                SELECT e.id, e.asunto_id, e.inbox_id, e.method, e.confidence, e.hito_date,
                       e.hito_text, e.status, e.created_at, i.occurred_at,
                       COALESCE(i.payload->>'subject', i.payload->>'filename',
                                i.payload->>'text', '') AS subject,
                       COALESCE(i.payload->'from'->>'address', i.payload->'from'->>'name',
                                i.payload->>'from',
                                CASE WHEN jsonb_typeof(i.payload->'sender') = 'object' THEN
                                    COALESCE(i.payload->'sender'->>'display_name',
                                             CASE WHEN i.payload->'sender'->>'is_self' = 'true'
                                                  THEN 'yo' END,
                                             i.payload->'sender'->>'phone')
                                ELSE i.payload->>'sender' END,
                                '') AS sender,
                       i.payload->>'filename' AS filename,
                       m.id AS media_id, m.size_bytes
                FROM mod_asuntos_evidence e
                JOIN inbox i ON i.id = e.inbox_id
                LEFT JOIN LATERAL (
                    SELECT id, size_bytes FROM media_assets
                    WHERE inbox_id = i.id ORDER BY id LIMIT 1
                ) m ON TRUE
                WHERE e.user_id = :u AND e.asunto_id = :a
                  AND (:all OR e.status = 'active')
                ORDER BY i.occurred_at, e.id
                """
            ),
            {"u": user_id, "a": asunto_id, "all": include_rejected},
        )
        .mappings()
        .all()
    )
    return [
        Evidence(
            id=int(r["id"]),
            asunto_id=int(r["asunto_id"]),
            inbox_id=int(r["inbox_id"]),
            method=str(r["method"]),
            confidence=float(r["confidence"]),
            hito_date=r["hito_date"],
            hito_text=r["hito_text"],
            status=str(r["status"]),
            occurred_at=r["occurred_at"],
            subject=str(r["subject"] or "")[:200],
            sender=str(r["sender"] or "")[:120],
            created_at=r["created_at"],
            filename=(str(r["filename"])[:200] if r["filename"] else None),
            media_id=(int(r["media_id"]) if r["media_id"] is not None else None),
            size_bytes=(int(r["size_bytes"]) if r["size_bytes"] is not None else None),
        )
        for r in rows
    ]


def evidence_inbox_ids(conn: Connection, user_id: int, asunto_id: int) -> list[int]:
    return [
        int(x)
        for x in conn.execute(
            text(
                "SELECT inbox_id FROM mod_asuntos_evidence WHERE user_id = :u AND asunto_id = :a "
                "AND status = 'active' ORDER BY inbox_id"
            ),
            {"u": user_id, "a": asunto_id},
        ).scalars()
    ]


def asuntos_of_inbox(conn: Connection, user_id: int, inbox_id: int) -> list[int]:
    return [
        int(x)
        for x in conn.execute(
            text(
                "SELECT asunto_id FROM mod_asuntos_evidence WHERE user_id = :u AND inbox_id = :i "
                "AND status = 'active'"
            ),
            {"u": user_id, "i": inbox_id},
        ).scalars()
    ]


def states_of(conn: Connection, user_id: int, asunto_id: int) -> list[StateRow]:
    rows = (
        conn.execute(
            text(
                "SELECT s.id, s.state_text, s.closed, s.source, s.inbox_id, "
                "COALESCE(i.occurred_at, s.created_at) AS created_at "
                "FROM mod_asuntos_states s LEFT JOIN inbox i ON i.id = s.inbox_id "
                "WHERE s.user_id = :u AND s.asunto_id = :a "
                "ORDER BY COALESCE(i.occurred_at, s.created_at), s.id"
            ),
            {"u": user_id, "a": asunto_id},
        )
        .mappings()
        .all()
    )
    return [
        StateRow(
            id=int(r["id"]),
            state_text=str(r["state_text"]),
            closed=bool(r["closed"]),
            source=str(r["source"]),
            inbox_id=int(r["inbox_id"]) if r["inbox_id"] else None,
            created_at=r["created_at"],
        )
        for r in rows
    ]


def relations_of(conn: Connection, user_id: int, asunto_id: int) -> list[RelationRow]:
    rows = (
        conn.execute(
            text(
                """
                SELECT r.id, r.src_asunto_id, r.dst_asunto_id, r.relation_type, r.confidence,
                       r.source,
                       CASE WHEN r.src_asunto_id = :a THEN r.dst_asunto_id ELSE r.src_asunto_id END
                           AS other_id,
                       CASE WHEN r.src_asunto_id = :a THEN 'out' ELSE 'in' END AS direction,
                       o.name AS other_name, o.status AS other_status
                FROM mod_asuntos_relations r
                JOIN mod_asuntos o
                  ON o.id = CASE WHEN r.src_asunto_id = :a THEN r.dst_asunto_id
                                 ELSE r.src_asunto_id END
                WHERE r.user_id = :u AND r.status = 'active'
                  AND (r.src_asunto_id = :a OR r.dst_asunto_id = :a)
                ORDER BY r.relation_type, r.id
                """
            ),
            {"u": user_id, "a": asunto_id},
        )
        .mappings()
        .all()
    )
    return [
        RelationRow(
            id=int(r["id"]),
            src_asunto_id=int(r["src_asunto_id"]),
            dst_asunto_id=int(r["dst_asunto_id"]),
            relation_type=str(r["relation_type"]),
            confidence=float(r["confidence"]),
            source=str(r["source"]),
            other_id=int(r["other_id"]),
            other_name=str(r["other_name"]),
            other_status=str(r["other_status"]),
            direction=str(r["direction"]),
        )
        for r in rows
    ]


def llm_relations(conn: Connection, user_id: int) -> list[tuple[int, int, int, str]]:
    """(id, src, dst, tipo) activas propuestas por el asignador (`source='llm'`): las revisa el
    curador. Las humanas/curadas no se re-preguntan."""
    return [
        (int(r[0]), int(r[1]), int(r[2]), str(r[3]))
        for r in conn.execute(
            text(
                "SELECT r.id, r.src_asunto_id, r.dst_asunto_id, r.relation_type "
                "FROM mod_asuntos_relations r "
                "JOIN mod_asuntos s ON s.id = r.src_asunto_id AND s.merged_into_id IS NULL "
                "JOIN mod_asuntos d ON d.id = r.dst_asunto_id AND d.merged_into_id IS NULL "
                "WHERE r.user_id = :u AND r.status = 'active' AND r.source = 'llm' ORDER BY r.id"
            ),
            {"u": user_id},
        )
    ]


def all_relations(conn: Connection, user_id: int) -> list[tuple[int, int, str]]:
    """(src, dst, tipo) activas del usuario — base del tejido asunto→asunto."""
    return [
        (int(r[0]), int(r[1]), str(r[2]))
        for r in conn.execute(
            text(
                "SELECT src_asunto_id, dst_asunto_id, relation_type FROM mod_asuntos_relations "
                "WHERE user_id = :u AND status = 'active'"
            ),
            {"u": user_id},
        )
    ]


# --- escritura ---------------------------------------------------------------------------- #


def create_asunto(
    conn: Connection,
    user_id: int,
    *,
    name: str,
    kind: str,
    description: str = "",
    seed: str = "otro",
    entities: Sequence[str] = (),
    opened_by_inbox_id: int | None = None,
    status: str = "candidato",
    priority: str = "normal",
    priority_reason: str = "",
    priority_source: str | None = None,
    due_on: date | None = None,
) -> int:
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind}")
    if priority not in PRIORITIES:
        raise ValueError(f"prioridad inválida: {priority}")
    if status not in STATUSES:
        raise ValueError(f"status inválido: {status}")
    aid = int(
        conn.execute(
            text(
                """
                INSERT INTO mod_asuntos
                    (user_id, name, kind, description, seed, entities, opened_by_inbox_id, status,
                     priority, priority_reason, priority_source, due_on)
                VALUES (:u, :n, :k, :d, :s, :e, :o, :st, :pr, :prr, :prs, :due)
                RETURNING id
                """
            ),
            {
                "u": user_id,
                "n": name.strip()[:160],
                "k": kind,
                "d": description.strip()[:600],
                "s": seed[:32],
                "e": [str(x).strip()[:80] for x in entities if str(x).strip()][:16],
                "o": opened_by_inbox_id,
                "st": status,
                "pr": priority,
                "prr": priority_reason.strip()[:300],
                "prs": priority_source,
                "due": due_on,
            },
        ).scalar_one()
    )
    if status in GRAPH_STATUSES:
        add_vertex(conn, user_id, Ref(ASUNTO_SLUG, aid))
    _log.info("asuntos.create", user_id=user_id, asunto_id=aid, kind=kind, status=status)
    return aid


def _touch(conn: Connection, user_id: int, asunto_id: int) -> None:
    conn.execute(
        text("UPDATE mod_asuntos SET updated_at = NOW() WHERE user_id = :u AND id = :a"),
        {"u": user_id, "a": asunto_id},
    )


def recount(conn: Connection, user_id: int, asunto_id: int) -> int:
    """Recalcula evidence_count/first/last_signal desde el libro (tras rechazos/fusiones). Si no
    queda evidencia, first/last_signal_at se CONSERVAN (ver comentario en el SQL)."""
    n = int(
        conn.execute(
            text(
                """
                WITH agg AS (
                    SELECT COUNT(*) AS n, MIN(i.occurred_at) AS f, MAX(i.occurred_at) AS l
                    FROM mod_asuntos_evidence e JOIN inbox i ON i.id = e.inbox_id
                    WHERE e.user_id = :u AND e.asunto_id = :a AND e.status = 'active'
                )
                UPDATE mod_asuntos m
                   SET evidence_count = agg.n,
                       -- sin evidencia (p. ej. vaciado por `force`) se conserva la ventana de
                       -- señal previa: así el asunto sigue siendo contemporáneo en el catálogo y
                       -- el modelo puede volver a elegirlo en vez de crear un duplicado.
                       first_signal_at = COALESCE(agg.f, m.first_signal_at),
                       last_signal_at = COALESCE(agg.l, m.last_signal_at),
                       updated_at = NOW()
                  FROM agg WHERE m.user_id = :u AND m.id = :a
                RETURNING m.evidence_count
                """
            ),
            {"u": user_id, "a": asunto_id},
        ).scalar_one()
    )
    return n


def add_evidence(
    conn: Connection,
    user_id: int,
    asunto_id: int,
    inbox_id: int,
    *,
    method: str,
    confidence: float = 1.0,
    hito_date: date | None = None,
    hito_text: str | None = None,
    reactivate: bool = False,
) -> bool | None:
    """Asienta evidencia (idempotente por (asunto, mensaje)); devuelve True si es NUEVA, False si ya
    existía activa y None si estaba RECHAZADA y se respetó el rechazo.

    Un rechazo (del usuario o del curador) es memoria: el asignador y la adhesión no lo pisan.
    Solo una alta explícita (`reactivate=True`: manual/agente) la vuelve activa. Un hito posterior
    sobre la misma evidencia la completa (no la duplica). Actualiza contadores y señales del asunto
    y marca el vértice sucio (si ya está en el grafo).
    """
    if method not in EVIDENCE_METHODS:
        raise ValueError(f"method inválido: {method}")
    if hito_text and _duplicate_hito(conn, user_id, asunto_id, inbox_id, hito_text):
        # Mismo hito ya asentado por otro mensaje (la misma invitación llegó por dos cuentas, o
        # dos correos marcan el mismo momento): la evidencia entra, el hito no se duplica.
        hito_text, hito_date = None, None
    row = (
        conn.execute(
            text(
                """
                INSERT INTO mod_asuntos_evidence
                    (user_id, asunto_id, inbox_id, method, confidence, hito_date, hito_text)
                VALUES (:u, :a, :i, :m, :c, :hd, :ht)
                ON CONFLICT (asunto_id, inbox_id) DO UPDATE
                   SET hito_date = CASE WHEN mod_asuntos_evidence.status = 'rejected' AND NOT :react
                                        THEN mod_asuntos_evidence.hito_date
                                        ELSE COALESCE(mod_asuntos_evidence.hito_date,
                                                      EXCLUDED.hito_date)
                                   END,
                       hito_text = CASE WHEN mod_asuntos_evidence.status = 'rejected' AND NOT :react
                                        THEN mod_asuntos_evidence.hito_text
                                        ELSE COALESCE(mod_asuntos_evidence.hito_text,
                                                      EXCLUDED.hito_text)
                                   END,
                       status = CASE WHEN mod_asuntos_evidence.status = 'rejected' AND NOT :react
                                     THEN 'rejected' ELSE 'active' END
                RETURNING (xmax = 0) AS created, status
                """
            ),
            {
                "u": user_id,
                "a": asunto_id,
                "i": inbox_id,
                "m": method,
                "c": max(0.0, min(1.0, float(confidence))),
                "hd": hito_date,
                "ht": (hito_text or "").strip()[:240] or None,
                "react": reactivate,
            },
        )
        .mappings()
        .one()
    )
    if str(row["status"]) == "rejected":
        return None
    created = bool(row["created"])
    recount(conn, user_id, asunto_id)
    st = conn.execute(
        text("SELECT status FROM mod_asuntos WHERE user_id = :u AND id = :a"),
        {"u": user_id, "a": asunto_id},
    ).scalar()
    if st in GRAPH_STATUSES:
        mark_dirty(conn, user_id, [Ref(ASUNTO_SLUG, asunto_id)])
    # Tareas EXTRAÍDAS de este mensaje que nacieron sueltas: ahora pertenecen a este asunto
    # (la extracción corre antes que la asignación). Import tardío: tareas importa relations.
    from memex.modules.tareas.module import adopt_orphan_tareas

    adopt_orphan_tareas(conn, user_id, asunto_id, inbox_id)

    return bool(created)


_HITO_NEAR_HOURS = 36


def _duplicate_hito(
    conn: Connection, user_id: int, asunto_id: int, inbox_id: int, hito_text: str
) -> bool:
    """¿Otro mensaje del mismo asunto ya marca este hito? Dos señales: el mismo texto normalizado,
    o una ENTREGA DUPLICADA (mismo asunto de correo y mismo dominio remitente a menos de 36 h: la
    misma invitación de Luma enviada a Gmail y a Outlook, con Message-ID distinto)."""
    return bool(
        conn.execute(
            text(
                rf"""
                SELECT 1
                FROM mod_asuntos_evidence e
                JOIN inbox i ON i.id = e.inbox_id
                JOIN inbox me ON me.id = :inbox
                WHERE e.user_id = :u AND e.asunto_id = :a AND e.status = 'active'
                  AND e.inbox_id <> :inbox AND e.hito_text IS NOT NULL
                  AND (
                    lower(regexp_replace(e.hito_text, '\W+', ' ', 'g')) = :norm
                    OR (
                      lower(COALESCE(i.payload->>'subject', ''))
                          = lower(COALESCE(me.payload->>'subject', ''))
                      AND split_part(lower(COALESCE(i.payload->'from'->>'address', '')), '@', 2)
                          = split_part(lower(COALESCE(me.payload->'from'->>'address', '')), '@', 2)
                      AND abs(extract(epoch FROM (i.occurred_at - me.occurred_at)))
                          <= {_HITO_NEAR_HOURS} * 3600
                    )
                  )
                LIMIT 1
                """
            ),
            {
                "u": user_id,
                "a": asunto_id,
                "inbox": inbox_id,
                "norm": re.sub(r"\W+", " ", hito_text.lower()).strip(),
            },
        ).first()
    )


def dedupe_hitos(conn: Connection, user_id: int) -> int:
    """Regla del sistema: por asunto, de dos evidencias que marcan el MISMO hito (texto igual o
    entrega duplicada) conserva el hito en la más antigua y lo quita de las demás (la evidencia
    queda). Devuelve cuántos hitos se retiraron."""
    rows = conn.execute(
        text(
            """
            SELECT e.id, e.asunto_id, e.hito_text, i.occurred_at,
                   lower(COALESCE(i.payload->>'subject', '')) AS subj,
                   split_part(lower(COALESCE(i.payload->'from'->>'address', '')), '@', 2) AS dom
            FROM mod_asuntos_evidence e JOIN inbox i ON i.id = e.inbox_id
            WHERE e.user_id = :u AND e.status = 'active' AND e.hito_text IS NOT NULL
            ORDER BY e.asunto_id, i.occurred_at, e.id
            """
        ),
        {"u": user_id},
    ).all()
    drop: list[int] = []
    seen_text: dict[tuple[int, str], bool] = {}
    seen_delivery: dict[tuple[int, str, str], datetime] = {}
    for eid, aid, txt, at, subj, dom in rows:
        norm = re.sub(r"\W+", " ", str(txt).lower()).strip()
        key_t = (int(aid), norm)
        key_d = (int(aid), str(subj), str(dom))
        prev = seen_delivery.get(key_d)
        near = (
            prev is not None
            and at is not None
            and abs((at - prev).total_seconds()) <= _HITO_NEAR_HOURS * 3600
        )
        if key_t in seen_text or near:
            drop.append(int(eid))
            continue
        seen_text[key_t] = True
        if at is not None:
            seen_delivery[key_d] = at
    if drop:
        conn.execute(
            text(
                "UPDATE mod_asuntos_evidence SET hito_text = NULL, hito_date = NULL "
                "WHERE user_id = :u AND id = ANY(:ids)"
            ),
            {"u": user_id, "ids": drop},
        )
    return len(drop)


def is_rejected(conn: Connection, user_id: int, asunto_id: int, inbox_id: int) -> bool:
    """¿El par (asunto, mensaje) fue rechazado? (memoria: la adhesión no lo vuelve a pegar)."""
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM mod_asuntos_evidence WHERE user_id = :u AND asunto_id = :a "
                "AND inbox_id = :i AND status = 'rejected'"
            ),
            {"u": user_id, "a": asunto_id, "i": inbox_id},
        ).first()
    )


def mark_evidence_reviewed(conn: Connection, user_id: int, evidence_ids: Sequence[int]) -> None:
    if not evidence_ids:
        return
    conn.execute(
        text(
            "UPDATE mod_asuntos_evidence SET reviewed_at = NOW() "
            "WHERE user_id = :u AND id = ANY(:ids)"
        ),
        {"u": user_id, "ids": list(evidence_ids)},
    )


@dataclass(frozen=True)
class SuspiciousEvidence:
    id: int
    asunto_id: int
    asunto_name: str
    asunto_kind: str
    inbox_id: int
    method: str
    confidence: float
    occurred_at: datetime | None
    sender: str
    subject: str
    snippet: str
    is_list: bool
    reason: str


def suspicious_evidence(
    conn: Connection, user_id: int, *, limit: int = 200, min_confidence: float = 0.75
) -> list[SuspiciousEvidence]:
    """Evidencia ACTIVA no revisada que merece una segunda mirada: confianza baja del asignador o
    correo de lista (newsletter) pegado a un asunto que no es comunidad/programa/hackathon."""
    rows = (
        conn.execute(
            text(
                """
                SELECT e.id, e.asunto_id, a.name, a.kind, e.inbox_id, e.method, e.confidence,
                       i.occurred_at,
                       COALESCE(i.payload->'from'->>'address', i.payload->>'from', '') AS sender,
                       COALESCE(i.payload->>'subject', '') AS subject,
                       left(COALESCE(i.payload->>'body_text', ''), 400) AS snippet,
                       (i.payload->>'list_id') IS NOT NULL AS is_list
                FROM mod_asuntos_evidence e
                JOIN mod_asuntos a ON a.id = e.asunto_id AND a.merged_into_id IS NULL
                JOIN inbox i ON i.id = e.inbox_id
                WHERE e.user_id = :u AND e.status = 'active' AND e.reviewed_at IS NULL
                  AND e.method = 'llm'
                  AND (e.confidence < :mc
                       OR ((i.payload->>'list_id') IS NOT NULL
                           AND a.kind NOT IN ('comunidad','programa','hackathon')))
                ORDER BY e.confidence, e.id
                LIMIT :lim
                """
            ),
            {"u": user_id, "mc": min_confidence, "lim": limit},
        )
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        conf = float(r["confidence"])
        reason = (
            f"confianza baja del asignador ({conf:.2f})"
            if conf < min_confidence
            else f"correo de lista pegado a un asunto tipo {r['kind']}"
        )
        out.append(
            SuspiciousEvidence(
                id=int(r["id"]),
                asunto_id=int(r["asunto_id"]),
                asunto_name=str(r["name"]),
                asunto_kind=str(r["kind"]),
                inbox_id=int(r["inbox_id"]),
                method=str(r["method"]),
                confidence=conf,
                occurred_at=r["occurred_at"],
                sender=str(r["sender"] or "")[:120],
                subject=str(r["subject"] or "")[:160],
                snippet=re.sub(r"\s+", " ", str(r["snippet"] or ""))[:300],
                is_list=bool(r["is_list"]),
                reason=reason,
            )
        )
    return out


def unreviewed_names(conn: Connection, user_id: int, *, limit: int = 200) -> list[Asunto]:
    """Asuntos con cuerpo (≥2 evidencias, no candidatos) cuyo nombre/tipo el curador no revisó aún o
    cambió desde la última revisión (`metadata.name_reviewed_name`)."""
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS} FROM mod_asuntos
                WHERE user_id = :u AND merged_into_id IS NULL AND status <> 'candidato'
                  AND evidence_count >= 2
                  AND (metadata->>'name_reviewed_name') IS DISTINCT FROM name
                ORDER BY evidence_count DESC, id LIMIT :lim
                """
            ),
            {"u": user_id, "lim": limit},
        )
        .mappings()
        .all()
    )
    return [_row_to_asunto(r) for r in rows]


RELATION_REVIEW_KINDS: frozenset[str] = frozenset(
    {"tramite", "programa", "hackathon", "proyecto", "evento", "comunidad"}
)


def unreviewed_relations(conn: Connection, user_id: int, *, limit: int = 200) -> list[Asunto]:
    """Asuntos con cuerpo cuyas relaciones el curador no revisó desde su última señal."""
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {_COLS} FROM mod_asuntos
                WHERE user_id = :u AND merged_into_id IS NULL AND status <> 'candidato'
                  AND evidence_count >= 2 AND kind = ANY(:kinds)
                  AND ((metadata->>'relations_reviewed_at') IS NULL
                       OR (metadata->>'relations_reviewed_at')::timestamptz < last_signal_at)
                ORDER BY evidence_count DESC, id LIMIT :lim
                """
            ),
            {"u": user_id, "kinds": sorted(RELATION_REVIEW_KINDS), "lim": limit},
        )
        .mappings()
        .all()
    )
    return [_row_to_asunto(r) for r in rows]


def mark_relations_reviewed(conn: Connection, user_id: int, asunto_id: int) -> None:
    conn.execute(
        text(
            "UPDATE mod_asuntos SET metadata = metadata || jsonb_build_object("
            "'relations_reviewed_at', to_jsonb(NOW())) WHERE user_id = :u AND id = :a"
        ),
        {"u": user_id, "a": asunto_id},
    )


def index_lines(conn: Connection, user_id: int) -> list[str]:
    """Índice compacto del catálogo (sin candidatos) para que el curador pueda relacionar un
    asunto con CUALQUIER otro, no solo con los del ítem."""
    return [
        f"A{a.id} · {a.name} · {a.kind} · {a.status} · {a.state_text[:90] or '—'}"
        for a in list_asuntos(
            conn, user_id, statuses=("abierto", "inactivo", "cerrado"), limit=2000
        )
    ]


def unreviewed_inactive(conn: Connection, user_id: int) -> list[int]:
    """Inactivos que el curador no revisó desde su última señal (¿terminó o sigue?)."""
    return [
        int(x)
        for x in conn.execute(
            text(
                "SELECT id FROM mod_asuntos WHERE user_id = :u AND merged_into_id IS NULL "
                "AND status = 'inactivo' AND ((metadata->>'inactive_reviewed_at') IS NULL "
                "OR (metadata->>'inactive_reviewed_at')::timestamptz < last_signal_at)"
            ),
            {"u": user_id},
        ).scalars()
    ]


def mark_inactive_reviewed(conn: Connection, user_id: int, asunto_id: int) -> None:
    conn.execute(
        text(
            "UPDATE mod_asuntos SET metadata = metadata || jsonb_build_object("
            "'inactive_reviewed_at', to_jsonb(NOW())) WHERE user_id = :u AND id = :a"
        ),
        {"u": user_id, "a": asunto_id},
    )


def mark_name_reviewed(conn: Connection, user_id: int, asunto_id: int) -> None:
    conn.execute(
        text(
            "UPDATE mod_asuntos SET metadata = metadata || jsonb_build_object("
            "'name_reviewed_name', name, 'name_reviewed_at', to_jsonb(NOW())) "
            "WHERE user_id = :u AND id = :a"
        ),
        {"u": user_id, "a": asunto_id},
    )


def reject_evidence(conn: Connection, user_id: int, evidence_id: int) -> int | None:
    """Rechaza una evidencia (queda como memoria: no se vuelve a proponer). Devuelve el asunto."""
    row = (
        conn.execute(
            text(
                "UPDATE mod_asuntos_evidence SET status = 'rejected' "
                "WHERE user_id = :u AND id = :e RETURNING asunto_id"
            ),
            {"u": user_id, "e": evidence_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    aid = int(row["asunto_id"])
    recount(conn, user_id, aid)
    return aid


def record_state(
    conn: Connection,
    user_id: int,
    asunto_id: int,
    state_text: str,
    *,
    closed: bool,
    source: str = "llm",
    inbox_id: int | None = None,
) -> None:
    """Asienta un estado (historial) y, si es el más RECIENTE, lo vuelve vigente; `closed` cierra.

    «Reciente» se mide por la fecha del MENSAJE que lo produjo (`inbox_id`), no por el orden en que
    se procesó: un reproceso o un backfill de correos viejos no puede pisar el estado que dejó un
    correo posterior. Sin mensaje (manual/curador) cuenta como ahora."""
    txt = state_text.strip()[:600]
    if not txt and not closed:
        return
    conn.execute(
        text(
            "INSERT INTO mod_asuntos_states "
            "(user_id, asunto_id, state_text, closed, source, inbox_id) "
            "VALUES (:u, :a, :t, :c, :s, :i)"
        ),
        {"u": user_id, "a": asunto_id, "t": txt, "c": closed, "s": source, "i": inbox_id},
    )
    if txt:
        conn.execute(
            text(
                """
                WITH eff AS (
                    SELECT COALESCE((SELECT occurred_at FROM inbox WHERE id = :i), NOW()) AS at
                )
                UPDATE mod_asuntos m
                   SET state_text = :t, state_updated_at = eff.at, updated_at = NOW()
                  FROM eff
                 WHERE m.user_id = :u AND m.id = :a
                   AND (m.state_updated_at IS NULL OR eff.at >= m.state_updated_at)
                """
            ),
            {"u": user_id, "a": asunto_id, "t": txt, "i": inbox_id},
        )
    if closed:
        set_status(conn, user_id, asunto_id, "cerrado", source=source)


def seed_state(conn: Connection, user_id: int, asunto_id: int) -> bool:
    """Estado INICIAL de un asunto que aún no tiene ninguno: el hito más temprano si lo hay
    («Cita en Cancillería agendada»), si no la descripción. Provisional: la primera actualización
    real (mensaje igual o posterior) lo pisa por la regla de `record_state`. Sin esto, un asunto
    nacido de un solo correo quedaba sin línea de «en qué va». Devuelve True si sembró."""
    row = conn.execute(
        text(
            "SELECT description, opened_by_inbox_id, COALESCE(state_text, '') AS st "
            "FROM mod_asuntos WHERE user_id = :u AND id = :a AND merged_into_id IS NULL"
        ),
        {"u": user_id, "a": asunto_id},
    ).first()
    if row is None or str(row[2]).strip():
        return False
    hito = conn.execute(
        text(
            """
            SELECT e.hito_text, e.inbox_id FROM mod_asuntos_evidence e
            LEFT JOIN inbox i ON i.id = e.inbox_id
            WHERE e.asunto_id = :a AND e.status = 'active' AND e.hito_text IS NOT NULL
              AND e.hito_text <> ''
            ORDER BY COALESCE(e.hito_date, i.occurred_at::date) NULLS LAST, e.id
            LIMIT 1
            """
        ),
        {"a": asunto_id},
    ).first()
    if hito is not None:
        txt, inbox_id = str(hito[0]), int(hito[1]) if hito[1] is not None else None
    else:
        txt, inbox_id = str(row[0] or ""), row[1]
    txt = txt.strip()
    if not txt:
        return False
    record_state(conn, user_id, asunto_id, txt, closed=False, source="system", inbox_id=inbox_id)
    _log.info(
        "asuntos.state.seeded", user_id=user_id, asunto_id=asunto_id, from_hito=hito is not None
    )
    return True


def seed_missing_states(conn: Connection, user_id: int) -> int:
    """Siembra el estado de TODOS los asuntos vivos que no tienen ninguno (regla del sistema,
    idempotente). Devuelve cuántos sembró."""
    ids = [
        int(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM mod_asuntos WHERE user_id = :u AND merged_into_id IS NULL "
                "AND COALESCE(state_text, '') = '' ORDER BY id"
            ),
            {"u": user_id},
        ).all()
    ]
    return sum(int(seed_state(conn, user_id, a)) for a in ids)


def rederive_state(conn: Connection, user_id: int, asunto_id: int) -> bool:
    """Estado vigente := el del historial con fecha efectiva más reciente (regla del sistema; repara
    lo asentado antes de ordenar por fecha del mensaje). Devuelve True si cambió."""
    n = conn.execute(
        text(
            """
            WITH last AS (
                SELECT s.state_text, COALESCE(i.occurred_at, s.created_at) AS at
                FROM mod_asuntos_states s LEFT JOIN inbox i ON i.id = s.inbox_id
                WHERE s.user_id = :u AND s.asunto_id = :a AND s.state_text <> ''
                ORDER BY COALESCE(i.occurred_at, s.created_at) DESC, s.id DESC LIMIT 1
            )
            UPDATE mod_asuntos m
               SET state_text = last.state_text, state_updated_at = last.at, updated_at = NOW()
              FROM last
             WHERE m.user_id = :u AND m.id = :a
               AND (m.state_text IS DISTINCT FROM last.state_text
                    OR m.state_updated_at IS DISTINCT FROM last.at)
            """
        ),
        {"u": user_id, "a": asunto_id},
    ).rowcount
    return bool(n)


def set_status(
    conn: Connection, user_id: int, asunto_id: int, status: str, *, source: str = "system"
) -> bool:
    """Cambia el ciclo. `candidato`→otro crea el vértice; cerrar/inactivar lo conserva (la historia
    sigue consultable y enlazada). Devuelve True si cambió.

    Un asunto SILENCIADO no lo revive nadie salvo el dueño: acá pasan el curador, el mantenimiento,
    la promoción de candidato y la fusión, así que este es el único lugar donde hay que sostenerlo.
    Si el dueño lo reabre a mano (`source` manual/agent), la marca se levanta: pidió verlo de nuevo.
    """
    if status not in STATUSES:
        raise ValueError(f"status inválido: {status}")
    cur = get_asunto(conn, user_id, asunto_id)
    if cur is None:
        return False
    if cur.silenced and status in ("abierto", "candidato"):
        if source not in ("manual", "agent"):
            _log.info(
                "asuntos.silenciado.no_se_reabre",
                asunto_id=asunto_id,
                intento=status,
                source=source,
            )
            return False
        unsilence(conn, user_id, asunto_id)
    if source in ("manual", "agent"):
        # La palabra del usuario queda sellada aunque el estado no cambie: protege al asunto de la
        # regla de inactividad y del curador hasta que llegue una señal nueva.
        conn.execute(
            text(
                "UPDATE mod_asuntos SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('manual_status_at', to_jsonb(NOW())) "
                "WHERE user_id = :u AND id = :a"
            ),
            {"u": user_id, "a": asunto_id},
        )
    if cur.status == status:
        return False
    conn.execute(
        text(
            "UPDATE mod_asuntos SET status = :s, "
            "closed_at = CASE WHEN :s IN ('cerrado','inactivo') THEN NOW() ELSE NULL END, "
            "updated_at = NOW() WHERE user_id = :u AND id = :a"
        ),
        {"u": user_id, "a": asunto_id, "s": status},
    )
    ref = Ref(ASUNTO_SLUG, asunto_id)
    if cur.status == "candidato" and status in GRAPH_STATUSES:
        add_vertex(conn, user_id, ref)
    elif status == "candidato" and cur.status in GRAPH_STATUSES:
        delete_vertex(conn, user_id, ref)
    else:
        mark_dirty(conn, user_id, [ref])
    _log.info(
        "asuntos.status",
        user_id=user_id,
        asunto_id=asunto_id,
        from_status=cur.status,
        to_status=status,
        source=source,
    )
    return True


#: Qué hacer con las pendientes de un asunto que se cierra a mano. `dejar` es el default
#: histórico (cerrar no tocaba nada) y sigue siendo el que no sorprende a nadie.
TAREAS_AL_CERRAR = ("dejar", "completar", "cancelar")
_TAREA_STATUS = {"completar": "completada", "cancelar": "cancelada"}


def resolve_pending_tareas(conn: Connection, user_id: int, asunto_id: int, accion: str) -> int:
    """Aplica `accion` a las tareas PENDIENTES del asunto. Devuelve cuántas cambiaron.

    Pasa por `tareas.set_status` y no por un UPDATE en bloque a propósito: ese sella `done_at` al
    completar y saca el vértice del grafo al cancelar. Import tardío por el ciclo (tareas importa
    relations, que conoce asuntos).
    """
    if accion not in TAREAS_AL_CERRAR:
        raise ValueError(f"acción inválida: {accion!r}; válidas: {', '.join(TAREAS_AL_CERRAR)}")
    nuevo = _TAREA_STATUS.get(accion)
    if nuevo is None:
        return 0
    from memex.modules.tareas.module import list_tareas
    from memex.modules.tareas.module import set_status as set_tarea_status

    pendientes = list_tareas(conn, user_id, asunto_id=asunto_id, status="pendiente", limit=500)
    for t in pendientes:
        set_tarea_status(conn, user_id, int(t["id"]), nuevo)
    if pendientes:
        _log.info(
            "asuntos.tareas_al_cerrar",
            asunto_id=asunto_id,
            accion=accion,
            tareas=len(pendientes),
        )
    return len(pendientes)


def silence(conn: Connection, user_id: int, asunto_id: int) -> tuple[bool, int]:
    """Silencia el asunto: lo cierra y lo deja cerrado. Devuelve (cambió, tareas canceladas).

    Cancela las tareas pendientes a propósito: silenciar es «no voy a hacer nada con esto», y dejar
    tareas vivas colgando de un asunto que no vuelve es la contradicción que se quiere evitar.
    """
    if get_asunto(conn, user_id, asunto_id) is None:
        return False, 0
    conn.execute(
        text(
            "UPDATE mod_asuntos SET metadata = COALESCE(metadata, '{}'::jsonb) "
            "|| jsonb_build_object('silenced_at', to_jsonb(NOW())), updated_at = NOW() "
            "WHERE user_id = :u AND id = :a"
        ),
        {"u": user_id, "a": asunto_id},
    )
    canceladas = resolve_pending_tareas(conn, user_id, asunto_id, "cancelar")
    set_status(conn, user_id, asunto_id, "cerrado", source="manual")
    _log.info("asuntos.silenciado", asunto_id=asunto_id, tareas_canceladas=canceladas)
    return True, canceladas


def unsilence(conn: Connection, user_id: int, asunto_id: int) -> bool:
    """Levanta el silencio. No reabre el asunto por sí solo: eso lo decide quien llame."""
    n = conn.execute(
        text(
            "UPDATE mod_asuntos SET metadata = metadata - 'silenced_at', updated_at = NOW() "
            "WHERE user_id = :u AND id = :a AND metadata->>'silenced_at' IS NOT NULL"
        ),
        {"u": user_id, "a": asunto_id},
    ).rowcount
    if n:
        _log.info("asuntos.silencio_levantado", asunto_id=asunto_id)
    return bool(n)


def promote_if_ready(
    conn: Connection, user_id: int, asunto_id: int, *, min_evidence_open: int
) -> bool:
    """`candidato` → `abierto` cuando junta evidencia suficiente o nació de una semilla fuerte."""
    a = get_asunto(conn, user_id, asunto_id)
    if a is None or a.status != "candidato":
        return False
    if a.evidence_count >= min_evidence_open or a.seed in STRONG_SEEDS:
        return set_status(conn, user_id, asunto_id, "abierto", source="system")
    return False


def set_due(
    conn: Connection,
    user_id: int,
    asunto_id: int,
    due_on: date | None,
    *,
    source: str = "llm",
) -> bool:
    """Fija (o borra, con None) la fecha límite del próximo paso. La del usuario (`source='user'`,
    sellada en `metadata.due_source`) no la pisa el modelo. Devuelve False si no cambió nada."""
    cur = get_asunto(conn, user_id, asunto_id)
    if cur is None:
        return False
    prev_source = conn.execute(
        text("SELECT metadata->>'due_source' FROM mod_asuntos WHERE user_id = :u AND id = :a"),
        {"u": user_id, "a": asunto_id},
    ).scalar()
    if prev_source == "user" and source != "user":
        return False
    if cur.due_on == due_on and prev_source == source:
        return False
    conn.execute(
        text(
            "UPDATE mod_asuntos SET due_on = :d, "
            "metadata = COALESCE(metadata, '{}'::jsonb) "
            "|| jsonb_build_object('due_source', CAST(:s AS TEXT)), "
            "updated_at = NOW() WHERE user_id = :u AND id = :a"
        ),
        {"u": user_id, "a": asunto_id, "d": due_on, "s": source},
    )
    _log.info(
        "asuntos.due",
        user_id=user_id,
        asunto_id=asunto_id,
        due_on=due_on.isoformat() if due_on else None,
        source=source,
    )
    return True


def set_priority(
    conn: Connection,
    user_id: int,
    asunto_id: int,
    priority: str,
    *,
    reason: str = "",
    source: str = "llm",
) -> bool:
    """Fija la prioridad. La palabra del usuario (`source='user'`) no la pisa ni el asignador ni la
    regla; devuelve False si no cambió nada."""
    if priority not in PRIORITIES:
        raise ValueError(f"prioridad inválida: {priority!r}; válidas: {', '.join(PRIORITIES)}")
    if source not in PRIORITY_SOURCES:
        raise ValueError(f"source inválido: {source!r}")
    cur = get_asunto(conn, user_id, asunto_id)
    if cur is None:
        return False
    if cur.priority_source == "user" and source != "user":
        return False
    if cur.priority == priority and cur.priority_source == source:
        return False
    conn.execute(
        text(
            "UPDATE mod_asuntos SET priority = :p, priority_reason = :r, priority_source = :s, "
            "updated_at = NOW() WHERE user_id = :u AND id = :a"
        ),
        {"u": user_id, "a": asunto_id, "p": priority, "r": reason.strip()[:300], "s": source},
    )
    mark_dirty(conn, user_id, [Ref(ASUNTO_SLUG, asunto_id)])
    _log.info(
        "asuntos.priority",
        user_id=user_id,
        asunto_id=asunto_id,
        from_priority=cur.priority,
        to_priority=priority,
        source=source,
    )
    return True


def reject_evidence_for(conn: Connection, user_id: int, asunto_id: int, inbox_id: int) -> bool:
    """Rechaza la evidencia (asunto, mensaje) si está activa: el asignador dijo que un mensaje
    adherido por llave es RUTINA (recibo normal, newsletter) y no pertenece al asunto."""
    eid = conn.execute(
        text(
            "SELECT id FROM mod_asuntos_evidence WHERE user_id = :u AND asunto_id = :a "
            "AND inbox_id = :i AND status = 'active'"
        ),
        {"u": user_id, "a": asunto_id, "i": inbox_id},
    ).scalar()
    if eid is None:
        return False
    return reject_evidence(conn, user_id, int(eid)) is not None


#: Un evento o hackatón del que NADIE dio una fecha (ni hito fechado ni plazo) y del que no se
#: habla hace tantos días ya pasó: se cierra solo. Los callers pasan `inactive_days // 2`.
DEFAULT_UNDATED_DAYS = 30


def close_expired(
    conn: Connection,
    user_id: int,
    *,
    grace_days: int = 7,
    today: date | None = None,
    undated_days: int = DEFAULT_UNDATED_DAYS,
) -> int:
    """Regla del sistema, tres vías: (1) un asunto de tipo con FECHA (evento, hackatón) cuyo último
    hito ya pasó hace más de `grace_days` y sin señal posterior → «terminó el <fecha>»; (2) un
    asunto de CUALQUIER tipo cuyo `due_on` (plazo del próximo paso) pasó hace más de `grace_days`
    sin señal posterior → «venció el <fecha> sin novedad»; (3) un evento/hackatón SIN ninguna fecha
    (el modelo no dio hito fechado ni plazo) y sin señal hace más de `undated_days` → «terminó (sin
    fecha conocida)»: un evento ocurre en una fecha, y si nadie la dio y hace semanas que no se
    habla de él, ya pasó; antes quedaba abierto para siempre (2026-09-17). Si después llega señal,
    el asignador lo reabre (los cerrados contemporáneos siguen en el catálogo). La palabra del
    usuario (estado manual posterior a la última señal) frena las tres. Devuelve cuántos cerró."""
    ref_day = today or date.today()
    n = _close_due(conn, user_id, grace_days=grace_days, ref_day=ref_day)
    n += _close_undated(conn, user_id, undated_days=undated_days, ref_day=ref_day)
    rows = conn.execute(
        text(
            """
            SELECT a.id, MAX(e.hito_date) AS last_hito, a.last_signal_at
            FROM mod_asuntos a
            JOIN mod_asuntos_evidence e ON e.asunto_id = a.id AND e.status = 'active'
            WHERE a.user_id = :u AND a.merged_into_id IS NULL
              AND a.status IN ('abierto', 'inactivo') AND a.kind = ANY(:kinds)
              AND e.hito_date IS NOT NULL
            GROUP BY a.id, a.last_signal_at
            HAVING MAX(e.hito_date) < :cut
            """
        ),
        {"u": user_id, "kinds": sorted(DATED_KINDS), "cut": ref_day - timedelta(days=grace_days)},
    ).all()
    for aid, last_hito, last_signal in rows:
        if last_signal is not None and last_signal.date() > last_hito + timedelta(days=grace_days):
            continue  # siguió habiendo señal después del «último» hito: no está terminado
        record_state(
            conn,
            user_id,
            int(aid),
            f"Terminó el {last_hito.isoformat()} (cierre automático por fecha).",
            closed=True,
            source="system",
        )
        n += 1
    if n:
        _log.info("asuntos.close_expired", user_id=user_id, closed=n)
    return n


def _close_undated(conn: Connection, user_id: int, *, undated_days: int, ref_day: date) -> int:
    rows = conn.execute(
        text(
            """
            SELECT a.id, a.last_signal_at
            FROM mod_asuntos a
            WHERE a.user_id = :u AND a.merged_into_id IS NULL
              AND a.status IN ('abierto', 'inactivo') AND a.kind = ANY(:kinds)
              AND a.due_on IS NULL AND a.last_signal_at IS NOT NULL
              AND a.last_signal_at::date < :cut
              AND COALESCE((a.metadata->>'manual_status_at')::timestamptz, '-infinity')
                  <= a.last_signal_at
              AND NOT EXISTS (SELECT 1 FROM mod_asuntos_evidence e
                               WHERE e.asunto_id = a.id AND e.status = 'active'
                                 AND e.hito_date IS NOT NULL)
            """
        ),
        {
            "u": user_id,
            "kinds": sorted(DATED_KINDS),
            "cut": ref_day - timedelta(days=undated_days),
        },
    ).all()
    for aid, last_signal in rows:
        record_state(
            conn,
            user_id,
            int(aid),
            f"Terminó (sin fecha conocida; sin señal desde el {last_signal.date().isoformat()}). "
            "Cierre automático.",
            closed=True,
            source="system",
        )
    if rows:
        _log.info("asuntos.close_undated", user_id=user_id, closed=len(rows))
    return len(rows)


def sweep_inactivity(
    conn: Connection, user_id: int, *, inactive_days: int, ref: datetime | None = None
) -> tuple[int, int]:
    """Regla del sistema: `abierto` sin señal `inactive_days` → `inactivo` (salvo que el usuario lo
    haya reabierto/cerrado a mano después de la última señal: su palabra vale hasta que llegue una
    nueva); `inactivo` con señal nueva → `abierto`. Devuelve (inactivados, reactivados). Corre al
    cierre de cada corrida del asignador y en el mantenimiento: un asunto que ya pasó no debe
    quedar abierto hasta que alguien corra el curador.

    `ref` = «ahora» de la medición (default NOW()). El asignador pasa la fecha del mensaje más
    nuevo de la corrida: al reprocesar un histórico (mensajes de mayo en septiembre) la inactividad
    se mide contra ese momento y no contra hoy, si no la adhesión determinista (solo a vivos)
    dejaría de pegar a mitad del backfill."""
    rows = conn.execute(
        text(
            "SELECT id, status FROM mod_asuntos WHERE user_id = :u AND merged_into_id IS NULL "
            "AND status IN ('abierto','inactivo') AND last_signal_at IS NOT NULL "
            "AND ((status = 'abierto' "
            "      AND last_signal_at < COALESCE(CAST(:ref AS timestamptz), NOW()) "
            "                           - make_interval(days => :d) "
            "      AND COALESCE((metadata->>'manual_status_at')::timestamptz, '-infinity') "
            "          <= last_signal_at) "
            "  OR (status = 'inactivo' "
            "      AND last_signal_at >= COALESCE(CAST(:ref AS timestamptz), NOW()) "
            "                            - make_interval(days => :d)))"
        ),
        {"u": user_id, "d": inactive_days, "ref": ref},
    ).all()
    inactivated = reactivated = 0
    for aid, status in rows:
        if status == "abierto":
            inactivated += int(set_status(conn, user_id, int(aid), "inactivo", source="system"))
        else:
            reactivated += int(set_status(conn, user_id, int(aid), "abierto", source="system"))
    if rows:
        _log.info(
            "asuntos.sweep_inactivity",
            user_id=user_id,
            inactivated=inactivated,
            reactivated=reactivated,
        )
    return inactivated, reactivated


def _close_due(conn: Connection, user_id: int, *, grace_days: int, ref_day: date) -> int:
    rows = conn.execute(
        text(
            """
            SELECT id, due_on, last_signal_at
            FROM mod_asuntos
            WHERE user_id = :u AND merged_into_id IS NULL
              AND status IN ('abierto', 'candidato', 'inactivo')
              AND due_on IS NOT NULL AND due_on < :cut
              AND COALESCE((metadata->>'manual_status_at')::timestamptz, '-infinity')
                  <= COALESCE(last_signal_at, '-infinity')
            """
        ),
        {"u": user_id, "cut": ref_day - timedelta(days=grace_days)},
    ).all()
    n = 0
    for aid, due_on, last_signal in rows:
        if last_signal is not None and last_signal.date() > due_on + timedelta(days=grace_days):
            continue  # hubo señal después del plazo: el asunto sigue vivo, el modelo decide
        record_state(
            conn,
            user_id,
            int(aid),
            f"Venció el {due_on.isoformat()} sin novedad (cierre automático por plazo).",
            closed=True,
            source="system",
        )
        n += 1
    return n


def clear_stale_due(
    conn: Connection, user_id: int, *, grace_days: int = 7, today: date | None = None
) -> int:
    """Regla del sistema: un `due_on` ya pasado hace más de `grace_days` con SEÑAL posterior al
    plazo no dice nada (el asunto siguió vivo después de «vencer»): se borra, salvo que lo haya
    fijado el usuario. Complementa a `close_expired`, que cierra los plazos vencidos SIN señal
    posterior."""
    ref_day = today or date.today()
    rows = conn.execute(
        text(
            """
            UPDATE mod_asuntos
               SET due_on = NULL,
                   metadata = COALESCE(metadata, '{}'::jsonb)
                              || jsonb_build_object('due_source', CAST('rule' AS TEXT)),
                   updated_at = NOW()
             WHERE user_id = :u AND merged_into_id IS NULL
               AND due_on IS NOT NULL AND due_on < :cut
               AND last_signal_at IS NOT NULL
               AND last_signal_at::date > due_on + make_interval(days => :g)
               AND COALESCE(metadata->>'due_source', '') <> 'user'
            RETURNING id
            """
        ),
        {"u": user_id, "cut": ref_day - timedelta(days=grace_days), "g": grace_days},
    ).all()
    n = len(rows)
    if n:
        _log.info("asuntos.due.cleared_stale", user_id=user_id, cleared=n)
    return n


def raise_priority_for_deadlines(
    conn: Connection, user_id: int, *, within_days: int = 14, today: date | None = None
) -> int:
    """Corroboración determinista: un asunto vivo con un hito FUTURO a `within_days` o menos pasa a
    prioridad alta (source `rule`). No toca lo que el usuario fijó. Devuelve cuántos subió."""
    ref_day = today or date.today()
    rows = conn.execute(
        text(
            """
            SELECT a.id, MIN(d.next_day) AS next_hito
            FROM mod_asuntos a
            JOIN LATERAL (
                SELECT e.hito_date AS next_day FROM mod_asuntos_evidence e
                WHERE e.asunto_id = a.id AND e.status = 'active' AND e.hito_date IS NOT NULL
                UNION ALL SELECT a.due_on WHERE a.due_on IS NOT NULL
            ) d ON d.next_day >= :today
            WHERE a.user_id = :u AND a.merged_into_id IS NULL
              AND a.status IN ('abierto', 'candidato') AND a.priority <> 'alta'
              AND COALESCE(a.priority_source, '') <> 'user'
            GROUP BY a.id
            HAVING MIN(d.next_day) <= :cut
            """
        ),
        {"u": user_id, "today": ref_day, "cut": ref_day + timedelta(days=within_days)},
    ).all()
    n = 0
    for aid, next_hito in rows:
        if set_priority(
            conn,
            user_id,
            int(aid),
            "alta",
            reason=f"hito el {next_hito.isoformat()} (a {(next_hito - ref_day).days} días)",
            source="rule",
        ):
            n += 1
    if n:
        _log.info("asuntos.priority.deadlines", user_id=user_id, raised=n)
    return n


def update_fields(
    conn: Connection,
    user_id: int,
    asunto_id: int,
    *,
    description: str | None = None,
    entities: Sequence[str] | None = None,
    kind: str | None = None,
) -> None:
    sets: list[str] = []
    params: dict[str, Any] = {"u": user_id, "a": asunto_id}
    if description is not None:
        sets.append("description = :d")
        params["d"] = description.strip()[:600]
    if entities is not None:
        sets.append(
            "entities = (SELECT ARRAY(SELECT DISTINCT x "
            "FROM unnest(entities || CAST(:e AS TEXT[])) x))"
        )
        params["e"] = [str(x).strip()[:80] for x in entities if str(x).strip()][:16]
    if kind is not None:
        if kind not in KINDS:
            raise ValueError(f"kind inválido: {kind}")
        sets.append("kind = :k")
        params["k"] = kind
    if not sets:
        return
    conn.execute(
        text(
            f"UPDATE mod_asuntos SET {', '.join(sets)}, updated_at = NOW() "
            "WHERE user_id = :u AND id = :a"
        ),
        params,
    )


def rename(conn: Connection, user_id: int, asunto_id: int, new_name: str) -> None:
    """Renombra conservando el nombre anterior como alias."""
    nn = new_name.strip()[:160]
    if not nn:
        raise ValueError("nombre vacío")
    conn.execute(
        text(
            """
            UPDATE mod_asuntos
               SET aliases = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(aliases || ARRAY[name]) x
                                           WHERE x <> :n)),
                   name = :n, updated_at = NOW()
             WHERE user_id = :u AND id = :a
            """
        ),
        {"u": user_id, "a": asunto_id, "n": nn},
    )
    st = conn.execute(
        text("SELECT status FROM mod_asuntos WHERE user_id = :u AND id = :a"),
        {"u": user_id, "a": asunto_id},
    ).scalar()
    if st in GRAPH_STATUSES:
        mark_dirty(conn, user_id, [Ref(ASUNTO_SLUG, asunto_id)])


def absorb_name(
    conn: Connection,
    user_id: int,
    asunto_id: int,
    name: str,
    *,
    entities: Sequence[str] = (),
) -> bool:
    """El asunto ya existe y llegó con OTRO nombre (gemelo detectado al asignar): el nombre nuevo
    queda como alias y sus entidades se suman; el nombre vigente no cambia. Devuelve True si algo
    cambió."""
    nn = " ".join(name.split())[:160]
    cur = get_asunto(conn, user_id, asunto_id)
    if cur is None or not nn:
        return False
    known = {normalize_name(cur.name), *(normalize_name(x) for x in cur.aliases)}
    new_alias = [] if normalize_name(nn) in known else [nn]
    have = {e.lower() for e in cur.entities}
    new_ents = [e.strip() for e in entities if e.strip() and e.strip().lower() not in have]
    if not new_alias and not new_ents:
        return False
    conn.execute(
        text(
            """
            UPDATE mod_asuntos
               SET aliases = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(aliases || :al) x
                                           WHERE x <> name)),
                   entities = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(entities || :en) x)),
                   updated_at = NOW()
             WHERE user_id = :u AND id = :a
            """
        ),
        {"u": user_id, "a": asunto_id, "al": new_alias, "en": new_ents},
    )
    if cur.status in GRAPH_STATUSES:
        mark_dirty(conn, user_id, [Ref(ASUNTO_SLUG, asunto_id)])
    return True


def add_relation(
    conn: Connection,
    user_id: int,
    src_asunto_id: int,
    dst_asunto_id: int,
    relation_type: str,
    *,
    confidence: float = 1.0,
    source: str = "llm",
) -> bool:
    """Relación dirigida src→dst (`parte_de`: src es parte de dst; `requisito_de`: src es requisito
    de dst; `continua`: src continúa a dst). Rechaza auto-relaciones y la inversa ya activa."""
    if relation_type not in RELATION_TYPES:
        raise ValueError(f"relation_type inválido: {relation_type}")
    src = resolve_merged(conn, user_id, src_asunto_id)
    dst = resolve_merged(conn, user_id, dst_asunto_id)
    if src == dst:
        return False
    reverse = conn.execute(
        text(
            "SELECT 1 FROM mod_asuntos_relations WHERE user_id = :u AND src_asunto_id = :d "
            "AND dst_asunto_id = :s AND relation_type = :t AND status = 'active'"
        ),
        {"u": user_id, "s": src, "d": dst, "t": relation_type},
    ).first()
    if reverse:
        return False
    created = conn.execute(
        text(
            """
            INSERT INTO mod_asuntos_relations
                (user_id, src_asunto_id, dst_asunto_id, relation_type, confidence, source)
            VALUES (:u, :s, :d, :t, :c, :src)
            ON CONFLICT (src_asunto_id, dst_asunto_id, relation_type) DO UPDATE
               SET status = CASE WHEN mod_asuntos_relations.status = 'rejected'
                                 THEN 'rejected' ELSE 'active' END,
                   confidence = GREATEST(mod_asuntos_relations.confidence, EXCLUDED.confidence)
            RETURNING (xmax = 0) AS created
            """
        ),
        {
            "u": user_id,
            "s": src,
            "d": dst,
            "t": relation_type,
            "c": max(0.0, min(1.0, confidence)),
            "src": source,
        },
    ).scalar_one()
    for aid in (src, dst):
        _touch(conn, user_id, aid)
    st = {
        int(r[0]): str(r[1])
        for r in conn.execute(
            text("SELECT id, status FROM mod_asuntos WHERE user_id = :u AND id = ANY(:ids)"),
            {"u": user_id, "ids": [src, dst]},
        )
    }
    refs = [Ref(ASUNTO_SLUG, a) for a in (src, dst) if st.get(a) in GRAPH_STATUSES]
    if refs:
        mark_dirty(conn, user_id, refs)
    return bool(created)


def reject_relation(conn: Connection, user_id: int, relation_id: int) -> bool:
    n = conn.execute(
        text("UPDATE mod_asuntos_relations SET status = 'rejected' WHERE user_id = :u AND id = :r"),
        {"u": user_id, "r": relation_id},
    ).rowcount
    return bool(n)


def merge_asuntos(conn: Connection, user_id: int, *, survivor_id: int, absorbed_id: int) -> bool:
    """Funde `absorbed` en `survivor`: evidencia, estados, notas, relaciones, llaves, alias y datos
    clave se mueven; el absorbido queda con `merged_into_id` (la historia no se pierde). El
    vértice se funde por `graph_writer.merge_vertices` (las aristas se re-apuntan)."""
    survivor_id = resolve_merged(conn, user_id, survivor_id)
    absorbed_id = resolve_merged(conn, user_id, absorbed_id)
    if survivor_id == absorbed_id:
        return False
    s, a = get_asunto(conn, user_id, survivor_id), get_asunto(conn, user_id, absorbed_id)
    if s is None or a is None:
        return False
    p = {"u": user_id, "s": survivor_id, "a": absorbed_id}
    conn.execute(
        text(
            "UPDATE mod_asuntos_evidence e SET asunto_id = :s "
            "WHERE e.user_id = :u AND e.asunto_id = :a "
            "AND NOT EXISTS (SELECT 1 FROM mod_asuntos_evidence x WHERE x.asunto_id = :s "
            "AND x.inbox_id = e.inbox_id)"
        ),
        p,
    )
    conn.execute(text("DELETE FROM mod_asuntos_evidence WHERE user_id = :u AND asunto_id = :a"), p)
    conn.execute(
        text("UPDATE mod_asuntos_states SET asunto_id = :s WHERE user_id = :u AND asunto_id = :a"),
        p,
    )
    conn.execute(
        text("UPDATE mod_asuntos_notas SET asunto_id = :s WHERE user_id = :u AND asunto_id = :a"),
        p,
    )
    conn.execute(
        text("UPDATE mod_asuntos_seen SET asunto_id = :s WHERE user_id = :u AND asunto_id = :a"),
        p,
    )
    conn.execute(
        text(
            "DELETE FROM mod_asuntos_relations WHERE user_id = :u AND "
            "((src_asunto_id = :a AND dst_asunto_id = :s) "
            "OR (src_asunto_id = :s AND dst_asunto_id = :a))"
        ),
        p,
    )
    for col in ("src_asunto_id", "dst_asunto_id"):
        conn.execute(
            text(
                f"UPDATE mod_asuntos_relations r SET {col} = :s "
                f"WHERE r.user_id = :u AND r.{col} = :a "
                "AND NOT EXISTS (SELECT 1 FROM mod_asuntos_relations x WHERE x.id <> r.id "
                f"AND x.{'dst_asunto_id' if col == 'src_asunto_id' else 'src_asunto_id'} = "
                f"r.{'dst_asunto_id' if col == 'src_asunto_id' else 'src_asunto_id'} "
                f"AND x.{col} = :s AND x.relation_type = r.relation_type)"
            ),
            p,
        )
    conn.execute(
        text(
            "DELETE FROM mod_asuntos_relations WHERE user_id = :u "
            "AND (src_asunto_id = :a OR dst_asunto_id = :a)"
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE mod_asuntos_keys k SET asunto_id = :s WHERE k.user_id = :u AND k.asunto_id = :a"
        ),
        p,
    )
    # Las TAREAS del absorbido pasan al superviviente (2026-09-25: se quedaban apuntando al
    # asunto fundido y la vista mostraba el nombre viejo; su arista la re-apunta merge_vertices).
    conn.execute(
        text(
            "UPDATE mod_tareas SET asunto_id = :s, updated_at = NOW() "
            "WHERE user_id = :u AND asunto_id = :a"
        ),
        p,
    )
    conn.execute(
        text(
            """
            UPDATE mod_asuntos
               SET aliases = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(aliases || :al) x
                                           WHERE x <> name)),
                   entities = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(entities || :en) x)),
                   description = CASE WHEN description = '' THEN :d ELSE description END,
                   state_text = CASE WHEN state_text = '' THEN :st ELSE state_text END,
                   opened_by_inbox_id = COALESCE(opened_by_inbox_id, :ob),
                   updated_at = NOW()
             WHERE user_id = :u AND id = :s
            """
        ),
        {
            **p,
            "al": [a.name, *a.aliases],
            "en": list(a.entities),
            "d": a.description,
            "st": a.state_text,
            "ob": a.opened_by_inbox_id,
        },
    )
    # Datos clave (metadata.datos): antes se quedaban en el absorbido y desaparecían de la vista.
    from memex.modules.asuntos.datos import merge_datos

    datos_moved = merge_datos(conn, user_id, survivor_id=survivor_id, absorbed_id=absorbed_id)
    conn.execute(
        text(
            "UPDATE mod_asuntos SET merged_into_id = :s, evidence_count = 0, updated_at = NOW() "
            "WHERE user_id = :u AND id = :a"
        ),
        p,
    )
    recount(conn, user_id, survivor_id)
    sref, aref = Ref(ASUNTO_SLUG, survivor_id), Ref(ASUNTO_SLUG, absorbed_id)
    if a.status in GRAPH_STATUSES and s.status in GRAPH_STATUSES:
        merge_vertices(conn, user_id, absorbed=aref, survivor=sref)
    elif a.status in GRAPH_STATUSES:
        # el superviviente era candidato: hereda el lugar en el grafo
        set_status(conn, user_id, survivor_id, "abierto", source="system")
        merge_vertices(conn, user_id, absorbed=aref, survivor=sref)
    elif s.status in GRAPH_STATUSES:
        mark_dirty(conn, user_id, [sref])
    _log.info(
        "asuntos.merge",
        user_id=user_id,
        survivor=survivor_id,
        absorbed=absorbed_id,
        datos_moved=datos_moved,
    )
    return True


def delete_asunto(conn: Connection, user_id: int, asunto_id: int) -> bool:
    """Borra un asunto (solo tiene sentido para candidatos/ruido). Las evidencias se borran; los
    mensajes quedan como «vistos sin asunto» para no re-preguntar."""
    a = get_asunto(conn, user_id, asunto_id)
    if a is None:
        return False
    if a.status in GRAPH_STATUSES:
        delete_vertex(conn, user_id, a.ref)
    conn.execute(
        text("DELETE FROM mod_asuntos WHERE user_id = :u AND id = :a"),
        {"u": user_id, "a": asunto_id},
    )
    _log.info("asuntos.delete", user_id=user_id, asunto_id=asunto_id, status=a.status)
    return True


# --- llaves de adhesión y marca «visto» --------------------------------------------------- #


def add_keys(
    conn: Connection, user_id: int, asunto_id: int, keys: Iterable[tuple[str, str]]
) -> int:
    """Registra llaves (kind, key) del asunto; la primera en reclamar una llave se la queda."""
    n = 0
    for kind, key in keys:
        if not key:
            continue
        n += conn.execute(
            text(
                "INSERT INTO mod_asuntos_keys (user_id, asunto_id, kind, key) "
                "VALUES (:u, :a, :k, :key) ON CONFLICT DO NOTHING"
            ),
            {"u": user_id, "a": asunto_id, "k": kind, "key": key[:300]},
        ).rowcount
    return n


def delete_keys(conn: Connection, user_id: int, keys: Iterable[tuple[str, str]]) -> int:
    """Borra llaves (reproceso: los mensajes olvidados dejan de sostener la adhesión)."""
    ks = [(k, v) for k, v in keys if v]
    if not ks:
        return 0
    return int(
        conn.execute(
            text(
                "DELETE FROM mod_asuntos_keys WHERE user_id = :u AND (kind, key) IN "
                "(SELECT kind, key FROM unnest(CAST(:kinds AS TEXT[]), CAST(:keys AS TEXT[])) "
                "AS t(kind, key))"
            ),
            {"u": user_id, "kinds": [k for k, _ in ks], "keys": [v[:300] for _, v in ks]},
        ).rowcount
    )


def find_by_keys(
    conn: Connection, user_id: int, keys: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """Llaves → asunto VIVO (candidato/abierto; los fundidos se resuelven al superviviente)."""
    ks = [(k, v) for k, v in keys if v]
    if not ks:
        return {}
    rows = conn.execute(
        text(
            """
            SELECT k.kind, k.key, k.asunto_id
            FROM mod_asuntos_keys k JOIN mod_asuntos m ON m.id = k.asunto_id
            WHERE k.user_id = :u AND m.status IN ('candidato','abierto')
              AND (k.kind, k.key) IN (SELECT kind, key FROM unnest(CAST(:kinds AS TEXT[]),
                                                                    CAST(:keys AS TEXT[]))
                                                          AS t(kind, key))
            """
        ),
        {"u": user_id, "kinds": [k for k, _ in ks], "keys": [v[:300] for _, v in ks]},
    )
    return {(str(r[0]), str(r[1])): int(r[2]) for r in rows}


def mark_seen(
    conn: Connection,
    user_id: int,
    decisions: Iterable[tuple[int, int | None, str]],
) -> None:
    for inbox_id, asunto_id, method in decisions:
        conn.execute(
            text(
                "INSERT INTO mod_asuntos_seen (user_id, inbox_id, asunto_id, method) "
                "VALUES (:u, :i, :a, :m) ON CONFLICT (user_id, inbox_id) DO UPDATE "
                "SET asunto_id = EXCLUDED.asunto_id, method = EXCLUDED.method, decided_at = NOW()"
            ),
            {"u": user_id, "i": inbox_id, "a": asunto_id, "m": method[:24]},
        )


def forget(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
    """Reproceso: borra la evidencia y la marca «visto» de esos mensajes (las llaves se regeneran al
    volver a decidir). Los candidatos que se quedan sin evidencia se eliminan acá; los demás los
    limpia `purge_empty` al TERMINAR la corrida con `force` (mientras tanto siguen en el catálogo
    para que el modelo pueda re-elegirlos)."""
    if not inbox_ids:
        return 0
    touched = [
        int(x)
        for x in conn.execute(
            text(
                "DELETE FROM mod_asuntos_evidence WHERE user_id = :u AND inbox_id = ANY(:ids) "
                "RETURNING asunto_id"
            ),
            {"u": user_id, "ids": list(inbox_ids)},
        ).scalars()
    ]
    conn.execute(
        text("DELETE FROM mod_asuntos_seen WHERE user_id = :u AND inbox_id = ANY(:ids)"),
        {"u": user_id, "ids": list(inbox_ids)},
    )
    # Los ESTADOS que esos mensajes produjeron también se olvidan: si el asignador los vuelve a
    # leer con otra regla y ya no emite estado, el vigente no puede seguir siendo el que dejaron.
    # El vigente vuelve a ser el más reciente de los que quedan (por fecha del mensaje).
    restated = [
        int(x)
        for x in conn.execute(
            text(
                "DELETE FROM mod_asuntos_states WHERE user_id = :u AND inbox_id = ANY(:ids) "
                "RETURNING asunto_id"
            ),
            {"u": user_id, "ids": list(inbox_ids)},
        ).scalars()
    ]
    for aid in sorted(set(restated)):
        restore_state(conn, user_id, aid)
    for aid in sorted(set(touched)):
        n = recount(conn, user_id, aid)
        a = get_asunto(conn, user_id, aid)
        if a is not None and n == 0 and a.status == "candidato":
            delete_asunto(conn, user_id, aid)
        # Los demás CONSERVAN su estado (cerrado/inactivo/abierto) mientras dura la corrida: el
        # modelo los ve así en el catálogo y decide si adhiere y si reabre. Las consecuencias de
        # lo que quedó (vacío, o abierto con poca evidencia) las asienta `settle_after_force`.
    return len(touched)


def restore_state(conn: Connection, user_id: int, asunto_id: int) -> str:
    """Vuelve vigente el estado más RECIENTE del historial que queda (por fecha del mensaje que lo
    produjo; sin mensaje, por cuándo se asentó), o lo deja vacío si no queda ninguno. Devuelve el
    texto vigente."""
    row = conn.execute(
        text(
            """
            SELECT s.state_text, COALESCE(i.occurred_at, s.created_at) AS at
            FROM mod_asuntos_states s LEFT JOIN inbox i ON i.id = s.inbox_id
            WHERE s.user_id = :u AND s.asunto_id = :a AND s.state_text <> ''
            ORDER BY at DESC, s.id DESC
            LIMIT 1
            """
        ),
        {"u": user_id, "a": asunto_id},
    ).first()
    txt = str(row[0]) if row else ""
    conn.execute(
        text(
            "UPDATE mod_asuntos SET state_text = :t, state_updated_at = :at, updated_at = NOW() "
            "WHERE user_id = :u AND id = :a"
        ),
        {"u": user_id, "a": asunto_id, "t": txt, "at": row[1] if row else None},
    )
    return txt


def asuntos_of(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> list[int]:
    """Asuntos con evidencia (de cualquier estado) de esos mensajes: lo que `force` va a tocar."""
    if not inbox_ids:
        return []
    return [
        int(x)
        for x in conn.execute(
            text(
                "SELECT DISTINCT asunto_id FROM mod_asuntos_evidence "
                "WHERE user_id = :u AND inbox_id = ANY(:ids) ORDER BY asunto_id"
            ),
            {"u": user_id, "ids": list(inbox_ids)},
        ).scalars()
    ]


def settle_after_force(
    conn: Connection, user_id: int, asunto_ids: Sequence[int], *, min_evidence_open: int
) -> tuple[int, int]:
    """Tras re-decidir con `force`, asienta lo que quedó de los asuntos que la corrida vació:
    - sin evidencia activa → se borra (era un cascarón: nadie lo volvió a elegir);
    - `abierto` con menos evidencia que `min_evidence_open` y sin semilla fuerte → `candidato`
      (tiene que re-ganarse la promoción; un solo correo no sostiene un asunto abierto);
    - `cerrado` / `inactivo` se conservan tal cual: el cierre previo (del modelo, del curador o
      por fecha) NO se pierde; solo el modelo, con una actualización `cerrado=false`, lo reabre.
    Lo que el usuario tocó (estado o prioridad manual) no se toca. Devuelve (borrados,
    degradados)."""
    purged = demoted = 0
    for aid in sorted(set(asunto_ids)):
        a = get_asunto(conn, user_id, aid)
        if a is None or a.merged_into_id is not None:
            continue
        if a.manual_status_at is not None or a.priority_source == "user":
            continue
        n = recount(conn, user_id, aid)
        if n == 0:
            if delete_asunto(conn, user_id, aid):
                purged += 1
        elif (
            a.status == "abierto" and n < min_evidence_open and a.seed not in STRONG_SEEDS
        ) and set_status(conn, user_id, aid, "candidato", source="system"):
            demoted += 1
    if purged or demoted:
        _log.info("asuntos.settle_after_force", user_id=user_id, purged=purged, demoted=demoted)
    return purged, demoted


def unseen_relevant_inbox_ids(conn: Connection, user_id: int, *, limit: int) -> list[int]:
    """Mensajes relevantes que el asignador aún no decidió, en orden cronológico."""
    return [
        int(x)
        for x in conn.execute(
            text(
                """
                SELECT i.id FROM inbox i
                JOIN sources src ON src.id = i.source_id
                LEFT JOIN mod_asuntos_seen s ON s.user_id = i.user_id AND s.inbox_id = i.id
                WHERE i.user_id = :u AND s.inbox_id IS NULL
                  AND (src.type <> ALL(:email_types) OR EXISTS (
                        SELECT 1 FROM relevance_verdicts v
                        WHERE v.inbox_id = i.id AND v.verdict = 'relevant'))
                ORDER BY i.occurred_at, i.id LIMIT :lim
                """
            ),
            {"u": user_id, "lim": limit, "email_types": EMAIL_TYPES},
        ).scalars()
    ]


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())
