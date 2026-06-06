"""Repositorio de `relation_edges`: la capa de ARISTAS del grafo (referencias entre vértices).

Disciplina (modelo v2):
- Guarda REFERENCIAS `(slug, id)` a vértices, nunca copia datos del módulo (ADR-015). CUALQUIER
  vértice puede conectarse con cualquiera: NO hay ontología que restrinja pares legales.
- Cada arista DEBE declarar su `producer` (quién la formó: `inbox`/`dedup`/`consolidacion`/
  `identidades`/`llm`/`humano`/...). Vocabulario ABIERTO: las constantes `PRODUCER_*` dan
  typo-safety sin cerrar el conjunto (igual que `capabilities`/`CAP_*`); NO se valida contra lista.
- `status` None = HECHO determinista (sin cola de revisión). Las inferencias nacen `pending` y
  transicionan con `resolve_edge` (monótono: `confirmed`/`rejected` son terminales).
- `propose_edge` es IDEMPOTENTE (ON CONFLICT sobre la UNIQUE lógica, que incluye el productor): el
  mismo productor re-corriendo no duplica ni pisa; distintos productores del mismo par coexisten.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger

_log = get_logger("memex.relations.edges")

# --- Nivel de la arista (dos tipos visibles + el descarte) --------------------------- #
STATUS_PISTA = "pista"  #: señal determinista NO vouchada (co-ocurrencia): "quizás se relacionan"
STATUS_CONFIRMED = "confirmed"  #: relación REAL, vouchada por dato/LLM/humano
STATUS_REJECTED = "rejected"  #: pista evaluada y descartada
#: Las pistas son los candidatos que el LLM evalúa → confirmed/rejected.
VALID_STATUS: frozenset[str] = frozenset({STATUS_PISTA, STATUS_CONFIRMED, STATUS_REJECTED})

# --- Productores (set ABIERTO; typo-safety, NO un gate de DB) ------------------------- #
PRODUCER_INBOX = "inbox"  #: el vértice nació de este mensaje (procedencia de ingesta)
PRODUCER_DEDUP = "dedup"  #: candidato de duplicado (p.ej. eventos crudos de calendar)
PRODUCER_CONSOLIDACION = "consolidacion"  #: el consolidado agrupa sus crudos de respaldo
PRODUCER_IDENTIDADES = "identidades"  #: resolución determinista de identidad (persona/org)
PRODUCER_FINANCE = "finance"  #: contraparte de un cobro/pago resuelta a una identidad
PRODUCER_LLM = "llm"  #: relación semántica / pertenencia a cúmulo decidida por el LLM
PRODUCER_HUMANO = "humano"  #: confirmada/creada por el usuario
PRODUCER_EVENT = "event"  #: hechos correlacionados por Hermes en un mismo mensaje (mismo event_id)
#: Conocidos (referencia); el `producer` real es texto libre — agregar uno NO requiere migración.
PRODUCERS: frozenset[str] = frozenset(
    {
        PRODUCER_INBOX,
        PRODUCER_DEDUP,
        PRODUCER_CONSOLIDACION,
        PRODUCER_IDENTIDADES,
        PRODUCER_FINANCE,
        PRODUCER_LLM,
        PRODUCER_HUMANO,
        PRODUCER_EVENT,
    }
)


@dataclass(frozen=True)
class Ref:
    """Referencia a un vértice: el slug de su tipo + el id de su fila.

    `slug` es la CLAVE DE DIRECCIONAMIENTO del grafo, no necesariamente el slug del módulo: codifica
    el subtipo cuando hace falta (`identidades:person`/`identidades:org`) y los vértices nativos del
    grafo (`cumulo`). Para calendar apunta al evento CONSOLIDADO."""

    slug: str
    id: int


@dataclass(frozen=True)
class RelationEdge:
    """Una arista materializada en `relation_edges`."""

    id: int
    user_id: int
    src: Ref
    dst: Ref
    relation_type: str
    producer: str
    confidence: Decimal | None
    evidence: str
    status: str
    seed_tag: str | None


def _row_to_edge(r: Any) -> RelationEdge:
    return RelationEdge(
        id=int(r["id"]),
        user_id=int(r["user_id"]),
        src=Ref(str(r["src_slug"]), int(r["src_id"])),
        dst=Ref(str(r["dst_slug"]), int(r["dst_id"])),
        relation_type=str(r["relation_type"]),
        producer=str(r["producer"]),
        confidence=r["confidence"],
        evidence=str(r["evidence"]),
        status=str(r["status"]),
        seed_tag=(str(r["seed_tag"]) if r["seed_tag"] is not None else None),
    )


def propose_edge(
    conn: Connection,
    user_id: int,
    src: Ref,
    dst: Ref,
    *,
    producer: str,
    relation_type: str = "",
    status: str = STATUS_PISTA,
    confidence: Decimal | None = None,
    evidence: str = "",
    seed_tag: str | None = None,
) -> int:
    """Materializa una arista (idempotente por la UNIQUE lógica) y devuelve su id.

    `producer` es OBLIGATORIO (quién forma la arista). `status` por defecto `pista` (señal sin
    vouchar, p.ej. co-ocurrencia); una relación REAL determinista (p.ej. persona↔org por dato) pasa
    `status='confirmed'`. NO valida ontología (cualquier par es legal). NO pisa una arista existente
    del mismo productor (idempotente); distintos productores del mismo par crean aristas
    independientes. `ValueError` si `producer` es vacío o `status` es inválido."""
    if not producer:
        raise ValueError("producer es obligatorio (quién formó la arista)")
    if status not in VALID_STATUS:
        raise ValueError(f"status inválido: {status!r}")

    params = {
        "uid": user_id,
        "ss": src.slug,
        "si": src.id,
        "ds": dst.slug,
        "di": dst.id,
        "rt": relation_type,
        "pr": producer,
        "conf": confidence,
        "ev": evidence,
        "st": status,
        "tag": seed_tag,
    }
    decided_at_sql = "NOW()" if status in ("confirmed", "rejected") else "NULL"
    new_id = conn.execute(
        text(
            f"""
            INSERT INTO relation_edges
              (user_id, src_slug, src_id, dst_slug, dst_id, relation_type, producer,
               confidence, evidence, status, decided_at, seed_tag)
            VALUES
              (:uid, :ss, :si, :ds, :di, :rt, :pr, :conf, :ev, :st, {decided_at_sql}, :tag)
            ON CONFLICT ON CONSTRAINT relation_edges_logical_uq DO NOTHING
            RETURNING id
            """
        ),
        params,
    ).scalar()
    if new_id is not None:
        return int(new_id)
    # Ya existía (mismo par + tipo + productor): devolver el id sin pisar la fila.
    existing = conn.execute(
        text(
            """
            SELECT id FROM relation_edges
            WHERE user_id = :uid AND src_slug = :ss AND src_id = :si
              AND dst_slug = :ds AND dst_id = :di AND relation_type = :rt AND producer = :pr
            """
        ),
        params,
    ).scalar_one()
    return int(existing)


def resolve_edge(
    conn: Connection,
    edge_id: int,
    *,
    status: str,
    producer: str,
    confidence: Decimal | None = None,
) -> bool:
    """Transiciona una PISTA → `confirmed`/`rejected` (monótono); devuelve si cambió algo.

    Una arista terminal NO se re-evalúa (WHERE status='pista'); el LLM o el humano deciden una sola
    vez. `producer` = quién resuelve (`llm`/`humano`). `confidence=None` deja el valor existente.
    (Las aristas ya `confirmed` por dato determinista no son pistas; no pasan por acá.)"""
    if status not in {STATUS_CONFIRMED, STATUS_REJECTED}:
        raise ValueError(f"resolve_edge solo a confirmed/rejected, no {status!r}")
    if not producer:
        raise ValueError("producer es obligatorio (quién resuelve la arista)")
    rows = conn.execute(
        text(
            """
            UPDATE relation_edges
            SET status = :st,
                producer = :pr,
                decided_at = NOW(),
                confidence = COALESCE(:conf, confidence)
            WHERE id = :id AND status = 'pista'
            """
        ),
        {"id": edge_id, "st": status, "pr": producer, "conf": confidence},
    ).rowcount
    if rows == 0:
        _log.info("relation.edge.resolve_noop", edge_id=edge_id, status=status)
    return rows > 0


def get_edge(
    conn: Connection, user_id: int, src: Ref, dst: Ref, *, producer: str, relation_type: str = ""
) -> RelationEdge | None:
    """La arista canónica `src-[relation_type/producer]->dst` del user, o `None`."""
    row = (
        conn.execute(
            text(
                """
                SELECT * FROM relation_edges
                WHERE user_id = :uid AND src_slug = :ss AND src_id = :si
                  AND dst_slug = :ds AND dst_id = :di AND relation_type = :rt AND producer = :pr
                """
            ),
            {
                "uid": user_id,
                "ss": src.slug,
                "si": src.id,
                "ds": dst.slug,
                "di": dst.id,
                "rt": relation_type,
                "pr": producer,
            },
        )
        .mappings()
        .first()
    )
    return _row_to_edge(row) if row is not None else None


def list_edges(
    conn: Connection,
    user_id: int,
    *,
    status: str | None = None,
    producer: str | None = None,
    seed_tag: str | None = None,
) -> list[RelationEdge]:
    """Aristas del user, opcionalmente filtradas por `status`, `producer` y/o `seed_tag` (para la
    vista del front, los asserts limpios, o acotar el seed en dev)."""
    rows = (
        conn.execute(
            text(
                """
                SELECT * FROM relation_edges
                WHERE user_id = :uid
                  AND (CAST(:status AS TEXT) IS NULL OR status = CAST(:status AS TEXT))
                  AND (CAST(:producer AS TEXT) IS NULL OR producer = CAST(:producer AS TEXT))
                  AND (CAST(:tag AS TEXT) IS NULL OR seed_tag = CAST(:tag AS TEXT))
                ORDER BY id
                """
            ),
            {"uid": user_id, "status": status, "producer": producer, "tag": seed_tag},
        )
        .mappings()
        .all()
    )
    return [_row_to_edge(r) for r in rows]


def list_pistas(conn: Connection, user_id: int) -> list[RelationEdge]:
    """La cola de candidatos: las PISTAS del user (señales por validar / promover a reales)."""
    return list_edges(conn, user_id, status=STATUS_PISTA)
