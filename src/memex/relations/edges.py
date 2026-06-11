"""Repositorio de `relation_edges`: la capa de ARISTAS del grafo (referencias entre vértices).

Disciplina (modelo v2):
- Guarda REFERENCIAS `(slug, id)` a vértices, nunca copia datos del módulo (ADR-015). CUALQUIER
  vértice puede conectarse con cualquiera: NO hay ontología que restrinja pares legales.
- Cada arista DEBE declarar su `producer` (quién la formó: `inbox`/`dedup`/`consolidacion`/
  `identidades`/`llm`/`humano`/...). Vocabulario ABIERTO: las constantes `PRODUCER_*` dan
  typo-safety sin cerrar el conjunto (igual que `capabilities`/`CAP_*`); NO se valida contra lista.
- `status` nace `pista` (DEFAULT NOT NULL en DB; señal determinista sin vouchar, p.ej.
  co-ocurrencia) y transiciona vía `resolve_edge` a `confirmed`/`rejected` (terminales, monótono).
  Las relaciones vouchadas (identidades/finance/llm/humano) se proponen directo en `confirmed`. No
  existe `None` ni `pending`.
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
PRODUCER_DEDUP = "dedup"  #: (placeholder, sin materializador) si se usa, referí consolidados
PRODUCER_CONSOLIDACION = "consolidacion"  #: (placeholder) crudo↔consolidado vive en mod_*_links
PRODUCER_IDENTIDADES = "identidades"  #: resolución determinista de identidad (persona/org)
PRODUCER_FINANCE = "finance"  #: contraparte de un cobro/pago resuelta a una identidad
PRODUCER_LLM = "llm"  #: relación semántica / pertenencia a cúmulo decidida por el LLM
PRODUCER_HUMANO = "humano"  #: confirmada/creada por el usuario
PRODUCER_EVENT = "event"  #: hechos correlacionados por Hermes en un mismo mensaje (mismo event_id)
PRODUCER_BIENESTAR = "bienestar"  #: un registro de bienestar cumple un hábito (match determinista)
PRODUCER_CANAL = "canal"  #: estructura del medio: quién escribe en qué canal (remitente resuelto)
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
        PRODUCER_BIENESTAR,
        PRODUCER_CANAL,
    }
)

# --- Tipos de relación con semántica fija (las demás `relation_type` son libres) ----- #
#: Pertenencia de un vértice a un CÚMULO: arista `miembro → cumulo` que materializa la membresía de
#: un cúmulo confirmado (la forma el validador LLM, `producer='llm'`). Distinta de `pertenece_a`
#: (jerarquía sub→padre de identidades). El paso de detección la EXCLUYE del grafo a clusterizar.
RELTYPE_MIEMBRO_DE = "miembro_de"
#: Tipo de relación de la co-ocurrencia (mismo mensaje); lo usan tanto la pista determinista como la
#: confirmación del LLM — por eso el peso de clusterización se decide por `(status, relation_type)`.
RELTYPE_COOCURRENCIA = "co-ocurrencia"
#: Slug del vértice NATIVO del grafo «cúmulo» (no sale de una tabla `mod_*`: lo proyecta
#: `relation_clusters`). Es el destino de las aristas `miembro_de`.
CUMULO_SLUG = "cumulo"
#: Participación en un canal de chat: arista REAL `persona → canal` (el remitente resuelto que
#: escribió ahí), `producer='canal'`, confirmed determinista.
RELTYPE_PARTICIPA_EN = "participa_en"
#: Slug del vértice «canal» (lo proyecta `mod_canales`, tabla del grafo derivada de inbox). El
#: canal NO cuenta para el cap de co-ocurrencia (es estructural, está en TODO mensaje del chat).
CANAL_SLUG = "canal"


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
    confidence: Decimal | None = None,
    evidence: str | None = None,
) -> bool:
    """Transiciona una PISTA → `confirmed`/`rejected` (monótono); devuelve si cambió algo.

    Una arista terminal NO se re-evalúa (WHERE status='pista'); el LLM/humano deciden una sola vez.
    NO toca `producer`: la pista la FORMÓ su productor (p.ej. `inbox`) y resolverla es un cambio de
    NIVEL, no una re-producción. Reescribir `producer` además CHOCARÍA con la UNIQUE lógica (que lo
    incluye) si ya existe una arista vouchada del mismo par por otro productor — p.ej. la
    co-ocurrencia que el LLM de identidades confirma directo (`producer='llm'`). Quién resolvió
    queda en `decided_at` + el cúmulo (la arista `miembro_de`). `confidence`/`evidence=None`
    dejan el valor existente. (las `confirmed` deterministas no son pistas; no pasan acá.)"""
    if status not in {STATUS_CONFIRMED, STATUS_REJECTED}:
        raise ValueError(f"resolve_edge solo a confirmed/rejected, no {status!r}")
    rows = conn.execute(
        text(
            """
            UPDATE relation_edges
            SET status = :st,
                decided_at = NOW(),
                confidence = COALESCE(:conf, confidence),
                evidence = COALESCE(:ev, evidence)
            WHERE id = :id AND status = 'pista'
            """
        ),
        {"id": edge_id, "st": status, "conf": confidence, "ev": evidence},
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
