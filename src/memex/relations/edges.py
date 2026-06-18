"""Repositorio de `relation_edges`: la capa de ARISTAS del grafo (referencias entre vértices).

Disciplina (modelo v2):
- Guarda REFERENCIAS `(slug, id)` a vértices, nunca copia datos del módulo (ADR-015). CUALQUIER
  vértice puede conectarse con cualquiera: NO hay ontología que restrinja pares legales.
- Cada arista DEBE declarar su `producer` (quién la formó: `inbox`/`dedup`/`consolidacion`/
  `identidades`/`llm`/`humano`/...). Vocabulario ABIERTO: las constantes `PRODUCER_*` dan
  typo-safety sin cerrar el conjunto (igual que `capabilities`/`CAP_*`); NO se valida contra lista.
- El NIVEL de la arista son DOS EJES: `provenance` (extracted/inferred — cómo lo sabemos) y
  `verdict` (confirmed/rejected/ambiguous — la decisión). Una co-ocurrencia nace
  `extracted+ambiguous` (la co-aparición es un hecho, la relación es sospecha sin juzgar) y
  transiciona vía `resolve_edge` (monótono, confirm gana). Las relaciones vouchadas deterministas
  (identidades/finance/...) se proponen directo `extracted+confirmed`. `relation` guarda la
  justificación corta vigente; `dirty` marca lo desactualizado (groundwork incremental, ADR-021).
- `propose_edge` es IDEMPOTENTE (ON CONFLICT sobre la UNIQUE lógica, que incluye el productor): el
  mismo productor re-corriendo no duplica ni pisa; distintos productores del mismo par coexisten.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger

_log = get_logger("memex.relations.edges")

# --- Nivel de la arista: DOS EJES ortogonales (procedencia por veredicto) --------------- #
# La procedencia viaja como parte del contrato de la arista (graphify EXTRACTED/INFERRED): se copia
# la disciplina, no el esquema. La etiqueta canónica visible se deriva de ambos (`canonical_label`).
PROVENANCE_EXTRACTED = "extracted"  #: leído literal de una fuente determinista — es un hecho
PROVENANCE_INFERRED = "inferred"  #: el LLM lo dedujo del contexto — conclusión, no textual
VALID_PROVENANCE: frozenset[str] = frozenset({PROVENANCE_EXTRACTED, PROVENANCE_INFERRED})

VERDICT_CONFIRMED = "confirmed"  #: relación REAL, vouchada por dato/LLM/humano
VERDICT_REJECTED = "rejected"  #: co-aparición casual, descartada
VERDICT_AMBIGUOUS = (
    "ambiguous"  #: sospecha sin decidir (antes 'pista'): sin juzgar, o la IA no supo
)
VALID_VERDICT: frozenset[str] = frozenset({VERDICT_CONFIRMED, VERDICT_REJECTED, VERDICT_AMBIGUOUS})


def canonical_label(provenance: str, verdict: str) -> str:
    """La etiqueta única que ve API/frontend/Hermes, derivada de los dos ejes."""
    if verdict == VERDICT_CONFIRMED:
        return "EXTRACTED" if provenance == PROVENANCE_EXTRACTED else "INFERRED"
    if verdict == VERDICT_REJECTED:
        return "INFERRED REJECTED" if provenance == PROVENANCE_INFERRED else "REJECTED"
    return "AMBIGUOUS (inferred)" if provenance == PROVENANCE_INFERRED else "AMBIGUOUS"


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
PRODUCER_CALENDAR = "calendar"  #: organizador/asistente de un evento resuelto a una identidad
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
        PRODUCER_CALENDAR,
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
#: Participación en un evento de calendario: aristas REALES `evento → identidad` para el ORGANIZADOR
#: («organiza») y los ASISTENTES («asiste»), resueltos por email contra el directorio.
#: `producer='calendar'`, confirmed determinista. El tipo va en `relation_type`; la dirección
#: (evento→identidad) es la del card y la del hermano `contraparte`.
RELTYPE_ORGANIZA = "organiza"
RELTYPE_ASISTE = "asiste"
#: Slug del vértice del evento de calendario (lo proyecta `mod_calendar_consolidated`, ver
#: `vertices.NODE_SOURCES`). La arista apunta al evento CONSOLIDADO, no al raw.
CALENDAR_SLUG = "calendar"


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
    provenance: str
    verdict: str
    relation: str
    dirty: bool
    seed_tag: str | None

    @property
    def label(self) -> str:
        """Etiqueta canónica derivada de los dos ejes (EXTRACTED/INFERRED/AMBIGUOUS/...)."""
        return canonical_label(self.provenance, self.verdict)


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
        provenance=str(r["provenance"]),
        verdict=str(r["verdict"]),
        relation=str(r["relation"]),
        dirty=bool(r["dirty"]),
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
    provenance: str = PROVENANCE_EXTRACTED,
    verdict: str = VERDICT_AMBIGUOUS,
    relation: str = "",
    confidence: Decimal | None = None,
    evidence: str = "",
    seed_tag: str | None = None,
) -> int:
    """Materializa una arista (idempotente por la UNIQUE lógica) y devuelve su id.

    `producer` es OBLIGATORIO (quién forma la arista). Por defecto nace `extracted+ambiguous` (una
    co-ocurrencia: la co-aparición es un hecho, la relación es sospecha sin juzgar). Una relación
    REAL determinista (p.ej. persona↔org por dato) pasa `verdict='confirmed'` (procedencia
    `extracted`). NO valida ontología (cualquier par es legal). NO pisa una arista existente del
    mismo productor (idempotente); distintos productores del mismo par crean aristas independientes.
    `ValueError` si `producer` es vacío o los ejes son inválidos."""
    if not producer:
        raise ValueError("producer es obligatorio (quién formó la arista)")
    if provenance not in VALID_PROVENANCE:
        raise ValueError(f"provenance inválida: {provenance!r}")
    if verdict not in VALID_VERDICT:
        raise ValueError(f"verdict inválido: {verdict!r}")

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
        "prov": provenance,
        "vd": verdict,
        "rel": relation,
        "tag": seed_tag,
    }
    decided_at_sql = "NOW()" if verdict in (VERDICT_CONFIRMED, VERDICT_REJECTED) else "NULL"
    new_id = conn.execute(
        text(
            f"""
            INSERT INTO relation_edges
              (user_id, src_slug, src_id, dst_slug, dst_id, relation_type, producer,
               confidence, evidence, provenance, verdict, relation, decided_at, seed_tag)
            VALUES
              (:uid, :ss, :si, :ds, :di, :rt, :pr, :conf, :ev, :prov, :vd, :rel,
               {decided_at_sql}, :tag)
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
    verdict: str,
    provenance: str,
    relation: str | None = None,
    confidence: Decimal | None = None,
    evidence: str | None = None,
) -> bool:
    """Transiciona el VEREDICTO de una arista (monótono, CONFIRM GANA); devuelve si cambió algo.

    - A `confirmed`: upgradea desde `ambiguous` Y desde `rejected` (un confirm posterior gana al
      reject) — guard `verdict <> 'confirmed'`; re-confirmar es no-op idempotente.
    - A `rejected`: SOLO desde `ambiguous` (no pisa `confirmed`) — guard `verdict='ambiguous'`.

    Setea `provenance` (quién lo decidió: extracted/inferred), `relation` (justificación corta) y
    marca `dirty=TRUE` (groundwork incremental). NO toca `producer`: la arista la FORMÓ su productor
    y resolverla es un cambio de NIVEL; reescribir `producer` chocaría con la UNIQUE lógica.
    `relation`/`confidence`/`evidence=None` dejan el valor existente."""
    if verdict not in {VERDICT_CONFIRMED, VERDICT_REJECTED}:
        raise ValueError(f"resolve_edge solo a confirmed/rejected, no {verdict!r}")
    if provenance not in VALID_PROVENANCE:
        raise ValueError(f"provenance inválida: {provenance!r}")
    guard = "verdict <> 'confirmed'" if verdict == VERDICT_CONFIRMED else "verdict = 'ambiguous'"
    rows = conn.execute(
        text(
            f"""
            UPDATE relation_edges
            SET verdict = :vd,
                provenance = :prov,
                decided_at = NOW(),
                dirty = TRUE,
                relation = COALESCE(:rel, relation),
                confidence = COALESCE(:conf, confidence),
                evidence = COALESCE(:ev, evidence)
            WHERE id = :id AND {guard}
            """
        ),
        {
            "id": edge_id,
            "vd": verdict,
            "prov": provenance,
            "rel": relation,
            "conf": confidence,
            "ev": evidence,
        },
    ).rowcount
    if rows == 0:
        _log.info("relation.edge.resolve_noop", edge_id=edge_id, verdict=verdict)
    return rows > 0


def mark_vertices_dirty(conn: Connection, user_id: int, refs: Sequence[Ref]) -> None:
    """Marca vértices como `dirty` en `relation_vertex_state` (upsert). Groundwork incremental
    (ADR-021): el productor avisa qué cambió para que un futuro mantenedor de cúmulos trabaje solo
    sobre el delta. La pueblan SOLO la capa grafo (build + fase de confirmación), no los módulos."""
    if not refs:
        return
    conn.execute(
        text(
            """
            INSERT INTO relation_vertex_state (user_id, slug, id, dirty, dirty_at)
            VALUES (:uid, :slug, :id, TRUE, NOW())
            ON CONFLICT (user_id, slug, id)
            DO UPDATE SET dirty = TRUE, dirty_at = NOW()
            """
        ),
        [{"uid": user_id, "slug": r.slug, "id": r.id} for r in dict.fromkeys(refs)],
    )


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
    verdict: str | None = None,
    provenance: str | None = None,
    producer: str | None = None,
    seed_tag: str | None = None,
) -> list[RelationEdge]:
    """Aristas del user, opcionalmente filtradas por `verdict`, `provenance`, `producer` y/o
    `seed_tag` (para la vista del front, los asserts limpios, o acotar el seed en dev)."""
    rows = (
        conn.execute(
            text(
                """
                SELECT * FROM relation_edges
                WHERE user_id = :uid
                  AND (CAST(:verdict AS TEXT) IS NULL OR verdict = CAST(:verdict AS TEXT))
                  AND (CAST(:prov AS TEXT) IS NULL OR provenance = CAST(:prov AS TEXT))
                  AND (CAST(:producer AS TEXT) IS NULL OR producer = CAST(:producer AS TEXT))
                  AND (CAST(:tag AS TEXT) IS NULL OR seed_tag = CAST(:tag AS TEXT))
                ORDER BY id
                """
            ),
            {
                "uid": user_id,
                "verdict": verdict,
                "prov": provenance,
                "producer": producer,
                "tag": seed_tag,
            },
        )
        .mappings()
        .all()
    )
    return [_row_to_edge(r) for r in rows]


def list_ambiguous(conn: Connection, user_id: int) -> list[RelationEdge]:
    """La cola de candidatos: las aristas AMBIGUAS del user (sospechas por juzgar / promover)."""
    return list_edges(conn, user_id, verdict=VERDICT_AMBIGUOUS)


def edges_touching(
    conn: Connection, user_id: int, ref: Ref, *, verdict: str | None = None
) -> list[RelationEdge]:
    """Las aristas con `ref` en CUALQUIER extremo (src o dst), con filtro opcional `verdict`.
    Para mostrar todas las relaciones de un vértice (p.ej. una identidad) sin importar la dirección.
    `list_edges` filtra por verdict/producer pero no por vértice; esto completa ese eje."""
    rows = (
        conn.execute(
            text(
                """
                SELECT * FROM relation_edges
                WHERE user_id = :uid
                  AND ((src_slug = :slug AND src_id = :id)
                    OR (dst_slug = :slug AND dst_id = :id))
                  AND (CAST(:verdict AS TEXT) IS NULL OR verdict = CAST(:verdict AS TEXT))
                ORDER BY id
                """
            ),
            {"uid": user_id, "slug": ref.slug, "id": ref.id, "verdict": verdict},
        )
        .mappings()
        .all()
    )
    return [_row_to_edge(r) for r in rows]
