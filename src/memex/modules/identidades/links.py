"""Libro de evidencia de vínculos + derivación del padre y del rol efectivos.

Antes la jerarquía era «último correo gana»: cada correo sobreescribía `parent_identity_id` sin
dejar rastro, y una variación del LLM pisaba lo que otros correos habían dicho o creaba una org
nueva por un nombre que no calzaba exacto. Acá el modelo es otro:

1. **Reclamos** (`mod_identidades_link_claims`): toda afirmación «A pertenece a B» / «P es miembro
   de O con rol R» se REGISTRA con su procedencia (correo, confianza, método) y nunca se pisa
   (`record_claim`, idempotente por correo+método). Los escriben el resolvedor por correo (`llm`),
   el organizador holístico (`organizer`), el curador codex (`curator`), la UI (`manual`), la CLI
   del agente (`agent`), la sincronización de contactos (`sync`) y la siembra (`legacy`).
2. **Padre efectivo derivado** (`derive_parent`): un padre pineado (manual/agent/curator) manda;
   si no, gana el candidato con más SOPORTE = suma de confianzas sobre mensajes distintos, que
   supere el umbral (≥ `parent_min_messages` mensajes, o 1 mensaje con confianza ≥
   `parent_single_min_confidence`); en empate se queda el actual (estabilidad). Un correo desviado
   no tumba lo que varios dijeron; varios correos coincidiendo forman la jerarquía. Anti-ciclo.
3. **Rol efectivo** (`effective_role`): la variante de rol más sustentada; las demás quedan
   visibles en el libro.
4. **Nombres sin inventar orgs** (`resolve_target_name`): un padre nombrado se resuelve por
   núcleo, nombre/alias normalizados y difuso ALTO; si no resuelve, el reclamo queda por NOMBRE
   (`target_id` NULL) para que el curador o el dueño decidan — ya no se crea una org por eso.

Solo escribe en `conn` (atómico con la tx del caller). Las aristas del grafo (`pertenece_a`,
`afiliado`) las teje el mismo punto que fija el dato (paso 5), como siempre.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.identidades.fuzzy import HIGH_THRESHOLD, find_fuzzy_candidates
from memex.relations.deterministic import weave_afiliacion, weave_pertenencia

_log = get_logger("memex.modules.identidades.links")

CLAIM_PERTENECE = "pertenece_a"
CLAIM_AFILIADO = "afiliado"
CLAIM_KINDS = (CLAIM_PERTENECE, CLAIM_AFILIADO)

METHOD_LLM = "llm"
METHOD_ORGANIZER = "organizer"
METHOD_CURATOR = "curator"
METHOD_MANUAL = "manual"
METHOD_AGENT = "agent"
METHOD_SYNC = "sync"
METHOD_LEGACY = "legacy"
METHODS = (
    METHOD_LLM,
    METHOD_ORGANIZER,
    METHOD_CURATOR,
    METHOD_MANUAL,
    METHOD_AGENT,
    METHOD_SYNC,
    METHOD_LEGACY,
)
#: Métodos cuyo padre queda PINEADO: la derivación automática no lo toca (el dueño o su agente
#: decidieron; el curador decide con evidencia + web y también pina, el dueño puede re-pinear).
PINNED_METHODS = frozenset({METHOD_MANUAL, METHOD_AGENT, METHOD_CURATOR})
#: `metadata.parent_source` de un padre pineado (los mismos valores que `method`).
PINNED_SOURCES = PINNED_METHODS
#: `parent_source` del padre que puso la derivación (no pineado).
DERIVED_SOURCE = "derived"

#: Mapa `source` (columna CHECK de identifiers/person_orgs) → `method` del libro, para los
#: escritores que hablan en términos de `source`.
METHOD_BY_SOURCE: dict[str, str] = {
    "manual": METHOD_MANUAL,
    "extraction": METHOD_LLM,
    "google_contacts": METHOD_SYNC,
}


@dataclass(frozen=True)
class Claim:
    id: int
    kind: str
    source_id: int
    target_id: int | None
    target_name: str
    role: str
    confidence: float
    method: str
    inbox_id: int | None
    status: str
    rationale: str
    created_at: Any


@dataclass(frozen=True)
class ParentCandidate:
    """Un padre propuesto para un hijo, agregado sobre sus reclamos activos."""

    target_id: int
    target_name: str
    messages: int  # mensajes distintos (un reclamo sin correo cuenta como uno)
    support: float  # suma de confianzas sobre esos mensajes
    best_confidence: float
    methods: tuple[str, ...]
    pinned: bool  # algún reclamo de método pineado


@dataclass(frozen=True)
class DeriveResult:
    child_id: int
    parent_id: int | None
    changed: bool
    reason: str  # pinned | derived | kept | no_candidate | cycle


@dataclass
class HierarchySettings:
    """Umbrales de derivación (perillas en `identidades_resolver_settings`)."""

    parent_min_messages: int = 2
    parent_single_min_confidence: float = 0.90
    min_confidence_parent: float = 0.80  # piso por reclamo para contar


_CLAIM_COLS = (
    "id, kind, source_id, target_id, target_name, role, confidence, method, inbox_id, status, "
    "rationale, created_at"
)


def _row_claim(r: Any) -> Claim:
    return Claim(
        id=int(r["id"]),
        kind=str(r["kind"]),
        source_id=int(r["source_id"]),
        target_id=int(r["target_id"]) if r["target_id"] is not None else None,
        target_name=str(r["target_name"] or ""),
        role=str(r["role"] or ""),
        confidence=float(r["confidence"]),
        method=str(r["method"]),
        inbox_id=int(r["inbox_id"]) if r["inbox_id"] is not None else None,
        status=str(r["status"]),
        rationale=str(r["rationale"] or ""),
        created_at=r["created_at"],
    )


# --- libro --------------------------------------------------------------------------------- #


def record_claim(
    conn: Connection,
    user_id: int,
    *,
    kind: str,
    source_id: int,
    target_id: int | None = None,
    target_name: str = "",
    role: str | None = None,
    confidence: float = 1.0,
    method: str,
    inbox_id: int | None = None,
    rationale: str = "",
) -> int | None:
    """Registra un reclamo (idempotente por correo+método: re-procesar no duplica; la confianza
    se queda con la mayor y el rol/rationale con el último no vacío). Devuelve el id, o None si el
    reclamo es inválido (sin destino, self-link, kind/método fuera de contrato)."""
    if kind not in CLAIM_KINDS or method not in METHODS:
        raise ValueError(f"reclamo fuera de contrato: kind={kind!r} method={method!r}")
    name = (target_name or "").strip()
    if target_id is None and not name:
        return None
    if target_id is not None and target_id == source_id:
        return None
    conf = max(0.0, min(1.0, float(confidence)))
    row = conn.execute(
        text(
            """
            INSERT INTO mod_identidades_link_claims
              (user_id, kind, source_id, target_id, target_name, role, confidence, method,
               inbox_id, rationale)
            VALUES (:u, :k, :s, :t, :tn, :role, :conf, :m, :inbox, :why)
            ON CONFLICT (user_id, kind, source_id, COALESCE(target_id, 0), target_name_norm,
                         COALESCE(inbox_id, 0), method)
            DO UPDATE SET
              confidence = GREATEST(mod_identidades_link_claims.confidence, EXCLUDED.confidence),
              role = CASE WHEN EXCLUDED.role <> '' THEN EXCLUDED.role
                          ELSE mod_identidades_link_claims.role END,
              rationale = CASE WHEN EXCLUDED.rationale <> '' THEN EXCLUDED.rationale
                               ELSE mod_identidades_link_claims.rationale END,
              status = 'active'
            RETURNING id
            """
        ),
        {
            "u": user_id,
            "k": kind,
            "s": source_id,
            "t": target_id,
            "tn": name,
            "role": (role or "").strip(),
            "conf": Decimal(str(round(conf, 3))),
            "m": method,
            "inbox": inbox_id,
            "why": rationale or "",
        },
    ).scalar_one()
    return int(row)


def list_claims(
    conn: Connection,
    user_id: int,
    *,
    source_id: int | None = None,
    target_id: int | None = None,
    kind: str | None = None,
    status: str | None = "active",
    inbox_id: int | None = None,
) -> list[Claim]:
    """Reclamos del user con filtros opcionales (por defecto solo los activos)."""
    where = ["user_id = :u"]
    params: dict[str, Any] = {"u": user_id}
    if source_id is not None:
        where.append("source_id = :s")
        params["s"] = source_id
    if target_id is not None:
        where.append("target_id = :t")
        params["t"] = target_id
    if kind is not None:
        where.append("kind = :k")
        params["k"] = kind
    if status is not None:
        where.append("status = :st")
        params["st"] = status
    if inbox_id is not None:
        where.append("inbox_id = :inbox")
        params["inbox"] = inbox_id
    rows = conn.execute(
        text(
            f"SELECT {_CLAIM_COLS} FROM mod_identidades_link_claims WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC"
        ),
        params,
    ).mappings()
    return [_row_claim(r) for r in rows]


def set_claim_status(conn: Connection, user_id: int, claim_id: int, status: str) -> Claim | None:
    """Rechaza (o reactiva) un reclamo. Devuelve el reclamo actualizado, o None si no existe."""
    if status not in ("active", "rejected"):
        raise ValueError(f"status inválido: {status!r}")
    r = (
        conn.execute(
            text(
                "UPDATE mod_identidades_link_claims SET status = :st WHERE id = :id AND "
                f"user_id = :u RETURNING {_CLAIM_COLS}"
            ),
            {"st": status, "id": claim_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    return _row_claim(r) if r is not None else None


def repoint_claims(conn: Connection, user_id: int, survivor_id: int, absorbed_id: int) -> int:
    """Al fusionar identidades: los reclamos del absorbido (como origen o destino) pasan al
    superviviente; los que chocarían con uno igual del superviviente se borran (queda el suyo);
    los que quedarían self-link se borran. Devuelve cuántos movió."""
    p = {"u": user_id, "surv": survivor_id, "absb": absorbed_id}
    conn.execute(
        text(
            """
            DELETE FROM mod_identidades_link_claims a
            WHERE a.user_id = :u AND a.source_id = :absb AND EXISTS (
              SELECT 1 FROM mod_identidades_link_claims s
              WHERE s.user_id = a.user_id AND s.kind = a.kind AND s.source_id = :surv
                AND COALESCE(s.target_id, 0) = COALESCE(a.target_id, 0)
                AND s.target_name_norm = a.target_name_norm
                AND COALESCE(s.inbox_id, 0) = COALESCE(a.inbox_id, 0) AND s.method = a.method)
            """
        ),
        p,
    )
    moved = conn.execute(
        text(
            "UPDATE mod_identidades_link_claims SET source_id = :surv "
            "WHERE user_id = :u AND source_id = :absb AND COALESCE(target_id, 0) <> :surv"
        ),
        p,
    ).rowcount
    conn.execute(
        text(
            """
            DELETE FROM mod_identidades_link_claims a
            WHERE a.user_id = :u AND a.target_id = :absb AND EXISTS (
              SELECT 1 FROM mod_identidades_link_claims s
              WHERE s.user_id = a.user_id AND s.kind = a.kind AND s.source_id = a.source_id
                AND s.target_id = :surv AND s.target_name_norm = a.target_name_norm
                AND COALESCE(s.inbox_id, 0) = COALESCE(a.inbox_id, 0) AND s.method = a.method)
            """
        ),
        p,
    )
    moved += conn.execute(
        text(
            "UPDATE mod_identidades_link_claims SET target_id = :surv "
            "WHERE user_id = :u AND target_id = :absb AND source_id <> :surv"
        ),
        p,
    ).rowcount
    # Lo que quedó apuntándose a sí mismo (origen o destino era el absorbido y el otro extremo el
    # superviviente) no es un vínculo: fuera.
    conn.execute(
        text(
            "DELETE FROM mod_identidades_link_claims WHERE user_id = :u "
            "AND (source_id = :absb OR target_id = :absb OR source_id = target_id)"
        ),
        p,
    )
    return int(moved)


# --- resolución de nombres (sin inventar orgs) --------------------------------------------- #


def resolve_target_name(
    conn: Connection, user_id: int, name: str, *, exclude_id: int | None = None
) -> int | None:
    """Resuelve el NOMBRE de un contenedor a una org/producto existente, en cascada determinista:
    núcleo de nombre (`org_core`) → nombre o alias normalizados → difuso ALTO (`HIGH_THRESHOLD`,
    el mismo umbral del auto-merge). Sin match → None: el reclamo queda por nombre; NO se crea
    nada (eso lo decide el curador o el dueño)."""
    n = (name or "").strip()
    if not n or "@" in n:
        return None
    params: dict[str, Any] = {"u": user_id, "n": n, "x": exclude_id if exclude_id else -1}
    hit = conn.execute(
        text(
            "SELECT id FROM mod_identidades WHERE user_id = :u AND id <> :x "
            "AND kind IN ('organizacion','producto') "
            "AND memex_org_core(:n) <> '' AND org_core = memex_org_core(:n) ORDER BY id LIMIT 1"
        ),
        params,
    ).scalar()
    if hit is not None:
        return int(hit)
    hit = conn.execute(
        text(
            """
            SELECT id FROM mod_identidades
            WHERE user_id = :u AND id <> :x AND kind IN ('organizacion','producto')
              AND memex_norm(:n) <> ''
              AND (name_norm = memex_norm(:n)
                   OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE memex_norm(a) = memex_norm(:n)))
            ORDER BY id LIMIT 1
            """
        ),
        params,
    ).scalar()
    if hit is not None:
        return int(hit)
    for kind in ("organizacion", "producto"):
        for c in find_fuzzy_candidates(
            conn, user_id, kind=kind, probe=n, limit=1, threshold=HIGH_THRESHOLD
        ):
            if c.identity_id != exclude_id:
                return c.identity_id
    return None


# --- derivación del padre ------------------------------------------------------------------- #


def load_hierarchy_settings(conn: Connection, user_id: int) -> HierarchySettings:
    row = (
        conn.execute(
            text(
                "SELECT parent_min_messages, parent_single_min_confidence, min_confidence_parent "
                "FROM identidades_resolver_settings WHERE user_id = :u"
            ),
            {"u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return HierarchySettings()
    return HierarchySettings(
        parent_min_messages=int(row["parent_min_messages"]),
        parent_single_min_confidence=float(row["parent_single_min_confidence"]),
        min_confidence_parent=float(row["min_confidence_parent"]),
    )


def parent_candidates(
    conn: Connection, user_id: int, child_id: int, *, min_confidence: float = 0.0
) -> list[ParentCandidate]:
    """Padres propuestos para `child_id`, agregados sobre sus reclamos `pertenece_a` activos con
    destino resuelto. `messages` = CORREOS distintos que lo sustentan (un reclamo sin correo —
    organizador, curador, siembra, manual— no es un correo: aporta al soporte y a la mejor
    confianza, pero no cuenta como mensaje). Soporte = suma de la mejor confianza por correo +
    la de cada reclamo sin correo. Ordenados por soporte desc, mensajes desc, id asc."""
    rows = conn.execute(
        text(
            """
            WITH por_msg AS (
              SELECT c.target_id, COALESCE(c.inbox_id, -c.id) AS msg,
                     bool_or(c.inbox_id IS NOT NULL) AS es_correo,
                     max(c.confidence) AS conf,
                     bool_or(c.method IN ('manual','agent','curator')) AS pinned,
                     array_agg(DISTINCT c.method) AS methods
              FROM mod_identidades_link_claims c
              WHERE c.user_id = :u AND c.kind = 'pertenece_a' AND c.source_id = :child
                AND c.status = 'active' AND c.target_id IS NOT NULL
                AND (c.confidence >= :minc OR c.method IN ('manual','agent','curator'))
              GROUP BY c.target_id, COALESCE(c.inbox_id, -c.id)
            )
            SELECT p.target_id, t.display_name,
                   count(*) FILTER (WHERE p.es_correo) AS messages, sum(p.conf) AS support,
                   max(p.conf) AS best, bool_or(p.pinned) AS pinned,
                   array_agg(DISTINCT m) AS methods
            FROM por_msg p
            JOIN mod_identidades t ON t.id = p.target_id
            CROSS JOIN LATERAL unnest(p.methods) AS m
            GROUP BY p.target_id, t.display_name
            ORDER BY support DESC, messages DESC, p.target_id ASC
            """
        ),
        {"u": user_id, "child": child_id, "minc": Decimal(str(min_confidence))},
    ).mappings()
    return [
        ParentCandidate(
            target_id=int(r["target_id"]),
            target_name=str(r["display_name"]),
            messages=int(r["messages"]),
            support=float(r["support"]),
            best_confidence=float(r["best"]),
            methods=tuple(sorted(str(m) for m in (r["methods"] or []))),
            pinned=bool(r["pinned"]),
        )
        for r in rows
    ]


def choose_parent(
    candidates: Sequence[ParentCandidate],
    *,
    current_parent: int | None,
    settings: HierarchySettings,
) -> int | None:
    """Regla PURA del padre efectivo (sin DB, testeable): un candidato pineado gana (el más
    reciente pin lo decide quien lo escribe; acá basta con que exista); si no, el de mayor soporte
    entre los que superan el umbral (≥ `parent_min_messages` mensajes, o 1 con confianza ≥
    `parent_single_min_confidence`); en empate de soporte se queda el actual si está empatado.
    Sin candidato que califique → el actual (estabilidad)."""
    pinned = [c for c in candidates if c.pinned]
    if pinned:
        return pinned[0].target_id
    qualified = [
        c
        for c in candidates
        if c.messages >= settings.parent_min_messages
        or c.best_confidence >= settings.parent_single_min_confidence
    ]
    if not qualified:
        return current_parent
    top = max(c.support for c in qualified)
    tied = [c for c in qualified if abs(c.support - top) < 1e-9]
    if current_parent is not None and any(c.target_id == current_parent for c in tied):
        return current_parent
    return tied[0].target_id


def _would_create_cycle(conn: Connection, user_id: int, child_id: int, parent_id: int) -> bool:
    """`parent_id` ya desciende de `child_id`? (sube la cadena de padres). Réplica local de
    `hierarchy.would_create_cycle` para no importar `hierarchy` (que importa este módulo)."""
    cur: int | None = parent_id
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        if cur == child_id:
            return True
        seen.add(cur)
        cur = conn.execute(
            text("SELECT parent_identity_id FROM mod_identidades WHERE id = :id AND user_id = :u"),
            {"id": cur, "u": user_id},
        ).scalar()
    return False


def merge_blocked_reason(conn: Connection, user_id: int, a: int, b: int) -> str | None:
    """Por qué una fusión AUTOMÁTICA (resolvedor) entre `a` y `b` NO debe aplicarse (None = puede):
    - `hierarchy`: una es ancestro de la otra («RAS Salesiana IEEE» ⊂ «Salesiana»). Una parte y su
      contenedor no son la misma entidad; el LLM confunde «pertenece a» con «es la misma» y, con
      un solo correo, colapsaba sub-unidades dentro de la universidad (prueba del 2026-09-05).
    - `pinned`: alguna tiene padre fijado por el dueño/agente/curador — ya se decidió que es una
      entidad propia; solo el curador o el dueño la fusionan (a propósito, por la UI/CLI).
    El curador y las vías manuales NO pasan por acá: la primitiva `merge_identities` sigue libre."""
    rows = conn.execute(
        text(
            "SELECT id, metadata->>'parent_source' AS src FROM mod_identidades "
            "WHERE user_id = :u AND id = ANY(:ids)"
        ),
        {"u": user_id, "ids": [a, b]},
    ).mappings()
    src = {int(r["id"]): r["src"] for r in rows}
    if a not in src or b not in src:
        return None
    if _would_create_cycle(conn, user_id, a, b) or _would_create_cycle(conn, user_id, b, a):
        return "hierarchy"
    if src[a] in PINNED_SOURCES or src[b] in PINNED_SOURCES:
        return "pinned"
    return None


def derive_parent(
    conn: Connection,
    user_id: int,
    child_id: int,
    *,
    settings: HierarchySettings | None = None,
) -> DeriveResult:
    """Recalcula el padre efectivo de `child_id` desde el libro y lo aplica (misma tx): respeta
    el pin (`metadata.parent_source` manual/agent/curator), salta candidatos que crearían ciclo,
    setea `parent_source='derived'` y teje `pertenece_a`. Incremental: solo este hijo."""
    st = settings or load_hierarchy_settings(conn, user_id)
    row = (
        conn.execute(
            text(
                "SELECT kind, parent_identity_id, metadata->>'parent_source' AS src "
                "FROM mod_identidades WHERE id = :id AND user_id = :u"
            ),
            {"id": child_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return DeriveResult(child_id, None, False, "missing")
    current = int(row["parent_identity_id"]) if row["parent_identity_id"] is not None else None
    if row["src"] in PINNED_SOURCES:
        return DeriveResult(child_id, current, False, "pinned")
    if row["kind"] not in ("organizacion", "producto"):
        return DeriveResult(child_id, current, False, "not_hangable")
    cands = parent_candidates(conn, user_id, child_id, min_confidence=st.min_confidence_parent)
    # Los que crearían ciclo se descartan ANTES de elegir (el siguiente mejor puede ser válido).
    safe = [c for c in cands if not _would_create_cycle(conn, user_id, child_id, c.target_id)]
    chosen = choose_parent(safe, current_parent=current, settings=st)
    if chosen == current:
        return DeriveResult(child_id, current, False, "kept" if cands else "no_candidate")
    if chosen is None:
        return DeriveResult(child_id, current, False, "kept")
    set_parent(conn, user_id, child_id, chosen, source=DERIVED_SOURCE)
    _log.info(
        "identidades.links.parent_derived",
        child_id=child_id,
        parent_id=chosen,
        previous=current,
        candidates=len(cands),
    )
    return DeriveResult(child_id, chosen, True, "derived")


def set_parent(
    conn: Connection, user_id: int, child_id: int, parent_id: int | None, *, source: str
) -> None:
    """Fija (o quita, `parent_id=None`) el padre con su `parent_source` y teje `pertenece_a`. La
    arista vieja (si el padre cambió) la limpia `reconcile_graph`. Único punto de escritura del
    padre para el libro: la derivación (`derived`), el curador y las vías manual/agent."""
    conn.execute(
        text(
            """
            UPDATE mod_identidades
            SET parent_identity_id = :p,
                metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{parent_source}',
                                     to_jsonb(CAST(:src AS TEXT))),
                updated_at = NOW()
            WHERE id = :c AND user_id = :u
            """
        ),
        {"p": parent_id, "c": child_id, "u": user_id, "src": source},
    )
    if parent_id is not None:
        weave_pertenencia(conn, user_id, child_id)


def claim_and_derive_parent(
    conn: Connection,
    user_id: int,
    *,
    child_id: int,
    target_id: int | None,
    target_name: str,
    confidence: float,
    method: str,
    inbox_id: int | None = None,
    rationale: str = "",
    settings: HierarchySettings | None = None,
) -> DeriveResult | None:
    """Atajo de los escritores automáticos (resolvedor, organizador): registra el reclamo — con
    el destino resuelto por nombre si no vino id — y re-deriva el padre del hijo. Devuelve None si
    el reclamo no era válido."""
    tid = target_id
    if tid is None and target_name:
        tid = resolve_target_name(conn, user_id, target_name, exclude_id=child_id)
    cid = record_claim(
        conn,
        user_id,
        kind=CLAIM_PERTENECE,
        source_id=child_id,
        target_id=tid,
        target_name=target_name,
        confidence=confidence,
        method=method,
        inbox_id=inbox_id,
        rationale=rationale,
    )
    if cid is None:
        return None
    if tid is None:
        _log.info(
            "identidades.links.parent_unresolved",
            child_id=child_id,
            target_name=target_name,
            claim_id=cid,
        )
        return DeriveResult(child_id, None, False, "unresolved_name")
    return derive_parent(conn, user_id, child_id, settings=settings)


# --- afiliación (persona → org) ------------------------------------------------------------- #


def effective_role(conn: Connection, user_id: int, person_id: int, org_id: int) -> str:
    """Rol efectivo de una afiliación: la variante (normalizada) con más mensajes distintos;
    empate → la más reciente. '' si ningún reclamo trae rol."""
    row = (
        conn.execute(
            text(
                """
                SELECT role, count(DISTINCT COALESCE(inbox_id, -id)) AS n, max(created_at) AS last
                FROM mod_identidades_link_claims
                WHERE user_id = :u AND kind = 'afiliado' AND source_id = :p AND target_id = :o
                  AND status = 'active' AND role <> ''
                GROUP BY role_norm, role
                ORDER BY n DESC, last DESC, role ASC
                LIMIT 1
                """
            ),
            {"u": user_id, "p": person_id, "o": org_id},
        )
        .mappings()
        .first()
    )
    return str(row["role"]) if row is not None else ""


def affiliate(
    conn: Connection,
    user_id: int,
    person_id: int,
    org_id: int,
    role: str | None,
    *,
    method: str,
    inbox_id: int | None = None,
    confidence: float = 1.0,
    rationale: str = "",
) -> int:
    """Registra el reclamo «persona es miembro de org (rol)» y asienta la afiliación efectiva en
    `person_orgs` con el ROL EFECTIVO (la variante más sustentada, no la última) + teje la arista
    `afiliado` en la misma tx. Ambos ids deben existir y ser distintos (el caller resuelve
    fusiones). Devuelve el id del reclamo."""
    if person_id == org_id:
        raise ValueError("una identidad no puede afiliarse a sí misma")
    cid = record_claim(
        conn,
        user_id,
        kind=CLAIM_AFILIADO,
        source_id=person_id,
        target_id=org_id,
        role=role,
        confidence=confidence,
        method=method,
        inbox_id=inbox_id,
        rationale=rationale,
    )
    assert cid is not None  # target_id no nulo y distinto de source → siempre válido
    eff = effective_role(conn, user_id, person_id, org_id)
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id, role, source)
            VALUES (:u, :p, :o, NULLIF(:role, ''), :src)
            ON CONFLICT (person_id, org_id) DO UPDATE SET role = NULLIF(EXCLUDED.role, '')
            """
        ),
        {
            "u": user_id,
            "p": person_id,
            "o": org_id,
            "role": eff,
            "src": _source_for(method),
        },
    )
    weave_afiliacion(conn, user_id, person_id)
    return cid


def _source_for(method: str) -> str:
    """`method` del libro → `source` permitido por el CHECK de `person_orgs`."""
    for src, m in METHOD_BY_SOURCE.items():
        if m == method:
            return src
    return "extraction"


# --- vista agregada (API / CLI) ------------------------------------------------------------- #


@dataclass
class LinkNode:
    """Una entrada colgable con el estado de su vínculo de pertenencia, para la vista."""

    id: int
    kind: str
    display_name: str
    parent_id: int | None
    parent_name: str | None
    parent_source: str | None
    pinned: bool
    messages: int  # soporte del padre ACTUAL (mensajes distintos)
    support: float
    best_confidence: float
    methods: tuple[str, ...]
    disputed: bool  # otro candidato que califica o con ≥ 50 % del soporte del actual
    weak: bool  # padre no pineado con < parent_min_messages mensajes
    unresolved_names: tuple[str, ...]  # padres nombrados sin resolver
    candidates: list[ParentCandidate] = field(default_factory=list)
    children_count: int = 0
    mention_count: int = 0
    created_by: str | None = None


def hierarchy_nodes(conn: Connection, user_id: int) -> list[LinkNode]:
    """Todas las entradas colgables (org/producto) con su vínculo evaluado contra el libro. Una
    consulta por agregados + una por candidatos (barato: el directorio es de cientos)."""
    st = load_hierarchy_settings(conn, user_id)
    rows = conn.execute(
        text(
            """
            SELECT i.id, i.kind, i.display_name, i.parent_identity_id AS parent_id,
                   p.display_name AS parent_name, i.metadata->>'parent_source' AS parent_source,
                   i.metadata->>'created_by' AS created_by,
                   (SELECT count(*) FROM mod_identidades c
                     WHERE c.parent_identity_id = i.id AND c.user_id = i.user_id) AS children,
                   (SELECT count(*) FROM mod_identidades_mentions m
                     WHERE m.resolved_identity_id = i.id AND m.user_id = i.user_id) AS mentions,
                   (SELECT array_agg(DISTINCT c.target_name)
                      FROM mod_identidades_link_claims c
                     WHERE c.user_id = i.user_id AND c.source_id = i.id AND c.kind = 'pertenece_a'
                       AND c.status = 'active' AND c.target_id IS NULL) AS unresolved
            FROM mod_identidades i
            LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id
            WHERE i.user_id = :u AND i.kind IN ('organizacion','producto')
            ORDER BY i.display_name
            """
        ),
        {"u": user_id},
    ).mappings()
    out: list[LinkNode] = []
    for r in rows:
        nid = int(r["id"])
        cands = parent_candidates(conn, user_id, nid, min_confidence=st.min_confidence_parent)
        parent_id = int(r["parent_id"]) if r["parent_id"] is not None else None
        cur = next((c for c in cands if c.target_id == parent_id), None)
        src = r["parent_source"]
        pinned = src in PINNED_SOURCES
        others = [c for c in cands if c.target_id != parent_id]
        cur_support = cur.support if cur else 0.0
        disputed = not pinned and any(
            (
                c.messages >= st.parent_min_messages
                or c.best_confidence >= st.parent_single_min_confidence
                or (cur_support > 0 and c.support >= 0.5 * cur_support)
            )
            for c in others
        )
        weak = (
            parent_id is not None
            and not pinned
            and (cur is None or cur.messages < st.parent_min_messages)
        )
        out.append(
            LinkNode(
                id=nid,
                kind=str(r["kind"]),
                display_name=str(r["display_name"]),
                parent_id=parent_id,
                parent_name=str(r["parent_name"]) if r["parent_name"] is not None else None,
                parent_source=str(src) if src else None,
                pinned=pinned,
                messages=cur.messages if cur else 0,
                support=cur.support if cur else 0.0,
                best_confidence=cur.best_confidence if cur else 0.0,
                methods=cur.methods if cur else (),
                disputed=disputed,
                weak=weak,
                unresolved_names=tuple(sorted(str(x) for x in (r["unresolved"] or []) if x)),
                candidates=cands,
                children_count=int(r["children"] or 0),
                mention_count=int(r["mentions"] or 0),
                created_by=str(r["created_by"]) if r["created_by"] else None,
            )
        )
    return out


@dataclass(frozen=True)
class AffiliationView:
    person_id: int
    person_name: str
    org_id: int
    org_name: str
    role: str
    messages: int
    roles: tuple[str, ...]  # variantes vistas


def affiliations_of(
    conn: Connection, user_id: int, *, org_id: int | None = None, person_id: int | None = None
) -> list[AffiliationView]:
    """Afiliaciones efectivas (person_orgs) con su soporte y variantes de rol del libro."""
    where = "po.user_id = :u"
    params: dict[str, Any] = {"u": user_id}
    if org_id is not None:
        where += " AND po.org_id = :o"
        params["o"] = org_id
    if person_id is not None:
        where += " AND po.person_id = :p"
        params["p"] = person_id
    rows = conn.execute(
        text(
            f"""
            SELECT po.person_id, pe.display_name AS person_name, po.org_id,
                   o.display_name AS org_name, COALESCE(po.role, '') AS role,
                   (SELECT count(DISTINCT COALESCE(c.inbox_id, -c.id))
                      FROM mod_identidades_link_claims c
                     WHERE c.user_id = po.user_id AND c.kind = 'afiliado' AND c.status = 'active'
                       AND c.source_id = po.person_id AND c.target_id = po.org_id) AS messages,
                   (SELECT array_agg(DISTINCT c.role)
                      FROM mod_identidades_link_claims c
                     WHERE c.user_id = po.user_id AND c.kind = 'afiliado' AND c.status = 'active'
                       AND c.source_id = po.person_id AND c.target_id = po.org_id
                       AND c.role <> '') AS roles
            FROM mod_identidades_person_orgs po
            JOIN mod_identidades pe ON pe.id = po.person_id
            JOIN mod_identidades o ON o.id = po.org_id
            WHERE {where}
            ORDER BY o.display_name, pe.display_name
            """
        ),
        params,
    ).mappings()
    return [
        AffiliationView(
            person_id=int(r["person_id"]),
            person_name=str(r["person_name"]),
            org_id=int(r["org_id"]),
            org_name=str(r["org_name"]),
            role=str(r["role"]),
            messages=int(r["messages"] or 0),
            roles=tuple(sorted(str(x) for x in (r["roles"] or []) if x)),
        )
        for r in rows
    ]


# --- siembra (estado previo al libro) ------------------------------------------------------- #


@dataclass
class SeedStats:
    parents: int = 0
    affiliations: int = 0
    mention_affiliations: int = 0
    derived_changed: int = 0


def seed_claims(conn: Connection, user_id: int) -> SeedStats:
    """Puebla el libro con lo que ya había: cada padre actual y cada `person_orgs` como reclamo
    `legacy` (sin correo), y cada MENCIÓN de persona resuelta que trae `org_hint`/`role_hint` como
    reclamo `llm` con su correo (evidencia real por mensaje, gratis). Idempotente. NO re-deriva
    padres (correr `derive` aparte; con solo legacy nada cambia)."""
    st = SeedStats()
    for r in conn.execute(
        text(
            "SELECT id, parent_identity_id AS p, COALESCE(metadata->>'parent_source','llm') AS src "
            "FROM mod_identidades WHERE user_id = :u AND parent_identity_id IS NOT NULL"
        ),
        {"u": user_id},
    ).mappings():
        method = str(r["src"]) if str(r["src"]) in PINNED_METHODS else METHOD_LEGACY
        if record_claim(
            conn,
            user_id,
            kind=CLAIM_PERTENECE,
            source_id=int(r["id"]),
            target_id=int(r["p"]),
            confidence=0.8,
            method=method,
            rationale="siembra: padre vigente antes del libro",
        ):
            st.parents += 1
    for r in conn.execute(
        text(
            "SELECT person_id, org_id, COALESCE(role, '') AS role, source "
            "FROM mod_identidades_person_orgs WHERE user_id = :u"
        ),
        {"u": user_id},
    ).mappings():
        src = str(r["source"])
        method = (
            METHOD_MANUAL
            if src == "manual"
            else METHOD_SYNC
            if src == "google_contacts"
            else METHOD_LEGACY
        )
        if record_claim(
            conn,
            user_id,
            kind=CLAIM_AFILIADO,
            source_id=int(r["person_id"]),
            target_id=int(r["org_id"]),
            role=str(r["role"]),
            confidence=0.8,
            method=method,
            rationale="siembra: afiliación vigente antes del libro",
        ):
            st.affiliations += 1
    # Menciones de persona con org/rol: evidencia por correo. La org se resuelve por nombre con la
    # misma cascada determinista (sin inventar).
    for r in conn.execute(
        text(
            """
            SELECT m.resolved_identity_id AS pid, m.org_hint, m.role_hint, m.confidence,
                   unnest(m.source_inbox_ids) AS inbox_id
            FROM mod_identidades_mentions m
            JOIN mod_identidades i ON i.id = m.resolved_identity_id
            WHERE m.user_id = :u AND i.kind = 'persona' AND m.org_hint <> ''
            """
        ),
        {"u": user_id},
    ).mappings():
        oid = resolve_target_name(conn, user_id, str(r["org_hint"]), exclude_id=int(r["pid"]))
        if oid is None:
            continue
        if record_claim(
            conn,
            user_id,
            kind=CLAIM_AFILIADO,
            source_id=int(r["pid"]),
            target_id=oid,
            role=str(r["role_hint"] or ""),
            confidence=float(r["confidence"] or 0.8),
            method=METHOD_LLM,
            inbox_id=int(r["inbox_id"]),
            rationale="siembra: mención con org/rol",
        ):
            st.mention_affiliations += 1
    return st


def derive_all(conn: Connection, user_id: int) -> list[DeriveResult]:
    """Re-deriva el padre de TODAS las entradas colgables (reparación / tras cambiar umbrales).
    Devuelve solo los cambios."""
    st = load_hierarchy_settings(conn, user_id)
    ids = [
        int(x)
        for x in conn.execute(
            text(
                "SELECT id FROM mod_identidades WHERE user_id = :u "
                "AND kind IN ('organizacion','producto') ORDER BY id"
            ),
            {"u": user_id},
        ).scalars()
    ]
    return [r for r in (derive_parent(conn, user_id, i, settings=st) for i in ids) if r.changed]
