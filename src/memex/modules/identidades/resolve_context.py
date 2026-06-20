"""Contexto por-correo para el resolvedor contextual (puro: solo lee la DB, sin LLM).

Tras extraer y persistir un correo, el resolvedor decide —con el contexto del correo— fusiones,
jerarquía y la disposición del remitente. Este módulo arma su ENTRADA y el predicado de SKIP:

- `email_needs_resolution`: ¿hay algo que disponer? Corre el LLM solo si el correo trae una
  identidad aún no resuelta-en-contexto (`metadata.resolved_context_at` ausente) o un remitente
  cuyo email todavía no es identificador de nadie (sin asociar). Si todo está resuelto/asociado →
  se salta (0 LLM): es el corazón incremental.
- `build_email_context`: cuerpo+asunto + las identidades del correo (extraídas + remitente) + los
  vecinos de trigrama de cada una (señal que el LLM puede confirmar/descartar con contexto). La
  co-ocurrencia (todas las identidades del correo juntas) es lo que ata, p.ej., la org-por-dominio
  con la org-por-nombre del mismo ente.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

from memex.modules.identidades.fuzzy import find_containment_candidates, find_fuzzy_candidates
from memex.modules.identidades.normalize import norm_identifier
from memex.processing.render import render_payload

#: Vecinos de trigrama por identidad nueva (cota holgada; el LLM filtra).
_CANDIDATE_LIMIT = 6
#: Cuerpo recortado que ve el LLM (señal de contexto, no el correo entero).
_BODY_CHARS = 2000
#: Topes de datos/jerarquía por identidad en el contexto (acota tokens; el resto se resume "+N").
_ID_ATTRS_CAP = 6
_CHILDREN_CAP = 8


def _capped(items: list[str], cap: int) -> tuple[str, ...]:
    """Primeros `cap` ítems; si hay más, agrega un marcador "…(+N)"."""
    if len(items) <= cap:
        return tuple(items)
    return (*items[:cap], f"…(+{len(items) - cap})")


@dataclass(frozen=True)
class EmailIdentity:
    """Una identidad que aparece en el correo: extraída del cuerpo o el remitente.

    Lleva la identidad REAL con sus DATOS (identificadores: email/dominio/handle son atributos de
    la identidad, no identidades) y su posición en la jerarquía (padre + hijos, si los hay), para
    que el resolvedor decida sobre la identidad consolidada, no sobre un nombre pelado."""

    identity_id: int
    kind: str
    display_name: str
    is_sender: bool
    sender_email: str | None  # email crudo del remitente (solo si `is_sender`)
    resolved_context: bool  # ya pasó por el resolvedor (`metadata.resolved_context_at`)
    identifiers: tuple[str, ...] = ()  # datos de la identidad ("email:x", "domain:y", "handle:z")
    parent_name: str | None = None  # padre en la jerarquía (si lo hay)
    children: tuple[str, ...] = ()  # hijos en la jerarquía (si los hay)


@dataclass(frozen=True)
class Candidate:
    """Una identidad del directorio candidata a fundir/ubicar respecto de las del correo."""

    identity_id: int
    kind: str
    display_name: str
    aliases: tuple[str, ...]
    domains: tuple[str, ...]  # identificadores `domain` (para el caso dominio↔nombre)
    parent_name: str | None
    score: float  # similitud de trigrama con la identidad del correo que lo trajo


@dataclass(frozen=True)
class ResolverInput:
    """Entrada del resolvedor para UN correo."""

    inbox_id: int
    subject: str
    body: str
    identities: tuple[EmailIdentity, ...]
    candidates: tuple[Candidate, ...]


def _identifiers_by_identity(
    conn: Connection, user_id: int, ids: list[int]
) -> dict[int, tuple[str, ...]]:
    """{identity_id → ("email:x", "domain:y", ...)} — los DATOS de cada identidad (acotados)."""
    if not ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT identity_id, kind, value_norm FROM mod_identidades_identifiers "
            "WHERE user_id = :u AND identity_id = ANY(:ids) ORDER BY identity_id, kind, value_norm"
        ),
        {"u": user_id, "ids": ids},
    ).mappings()
    acc: dict[int, list[str]] = {}
    for r in rows:
        acc.setdefault(int(r["identity_id"]), []).append(f"{r['kind']}:{r['value_norm']}")
    return {k: _capped(v, _ID_ATTRS_CAP) for k, v in acc.items()}


def _children_by_identity(
    conn: Connection, user_id: int, ids: list[int]
) -> dict[int, tuple[str, ...]]:
    """{parent_id → (nombre_hijo, ...)} — la jerarquía hacia abajo de cada identidad (acotada)."""
    if not ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT parent_identity_id AS pid, display_name FROM mod_identidades "
            "WHERE user_id = :u AND parent_identity_id = ANY(:ids) ORDER BY parent_identity_id, id"
        ),
        {"u": user_id, "ids": ids},
    ).mappings()
    acc: dict[int, list[str]] = {}
    for r in rows:
        acc.setdefault(int(r["pid"]), []).append(str(r["display_name"]))
    return {k: _capped(v, _CHILDREN_CAP) for k, v in acc.items()}


def _email_identities(conn: Connection, user_id: int, inbox_id: int) -> list[EmailIdentity]:
    """Identidades resueltas del correo (extraídas + remitente), dedup por id, con sus DATOS
    (identificadores) y posición en la jerarquía (padre + hijos). `is_sender` True si ALGUNA
    mención de esa identidad fue el remitente."""
    result = conn.execute(
        text(
            """
            SELECT m.resolved_identity_id AS id, i.kind, i.display_name,
                   bool_or(m.resolution_method = 'sender') AS is_sender,
                   max(CASE WHEN m.resolution_method = 'sender' THEN m.email END) AS sender_email,
                   bool_or(i.metadata->>'resolved_context_at' IS NOT NULL) AS resolved_ctx,
                   max(p.display_name) AS parent_name
            FROM mod_identidades_mentions m
            JOIN mod_identidades i ON i.id = m.resolved_identity_id
            LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id
            WHERE m.user_id = :uid AND :inbox = ANY(m.source_inbox_ids)
              AND m.resolved_identity_id IS NOT NULL
            GROUP BY m.resolved_identity_id, i.kind, i.display_name
            ORDER BY m.resolved_identity_id
            """
        ),
        {"uid": user_id, "inbox": inbox_id},
    )
    rows = result.mappings().all()
    ids = [int(r["id"]) for r in rows]
    attrs = _identifiers_by_identity(conn, user_id, ids)
    kids = _children_by_identity(conn, user_id, ids)
    return [
        EmailIdentity(
            identity_id=int(r["id"]),
            kind=str(r["kind"]),
            display_name=str(r["display_name"]),
            is_sender=bool(r["is_sender"]),
            sender_email=str(r["sender_email"]) if r["sender_email"] is not None else None,
            resolved_context=bool(r["resolved_ctx"]),
            identifiers=attrs.get(int(r["id"]), ()),
            parent_name=str(r["parent_name"]) if r["parent_name"] is not None else None,
            children=kids.get(int(r["id"]), ()),
        )
        for r in rows
    ]


def _sender_email_unassociated(conn: Connection, user_id: int, idents: list[EmailIdentity]) -> bool:
    """True si el correo tiene un remitente cuyo email todavía NO es identificador de ninguna
    identidad — la señal de «remitente nuevo sin asociar» que el resolvedor debe disponer."""
    for ident in idents:
        if not ident.is_sender or not ident.sender_email:
            continue
        vn = norm_identifier("email", ident.sender_email)
        if not vn:
            continue
        exists = conn.execute(
            text(
                "SELECT 1 FROM mod_identidades_identifiers "
                "WHERE user_id = :uid AND kind = 'email' AND value_norm = :vn LIMIT 1"
            ),
            {"uid": user_id, "vn": vn},
        ).first()
        if exists is None:
            return True
    return False


def email_needs_resolution(conn: Connection, user_id: int, inbox_id: int) -> bool:
    """Predicado de SKIP: ¿hay algo nuevo que disponer en este correo? (sin LLM)."""
    idents = _email_identities(conn, user_id, inbox_id)
    if not idents:
        return False
    if any(not i.resolved_context for i in idents):
        return True
    return _sender_email_unassociated(conn, user_id, idents)


def _candidates_for(conn: Connection, user_id: int, idents: list[EmailIdentity]) -> list[Candidate]:
    """Vecinos de trigrama (fuzzy + contención) de las identidades NO resueltas del correo, con su
    jerarquía/alias/dominios — lo que el LLM confirma o descarta con contexto. Dedup por id; excluye
    las identidades del propio correo (ya van como `identities`)."""
    own_ids = {i.identity_id for i in idents}
    by_id: dict[int, tuple[float, str]] = {}  # id → (mejor score, kind sondeado)
    for ident in idents:
        if ident.resolved_context or ident.kind == "desconocido":
            continue
        probe_kind = "persona" if ident.kind == "persona" else ident.kind
        neighbors = [
            *find_fuzzy_candidates(
                conn, user_id, kind=probe_kind, probe=ident.display_name, limit=_CANDIDATE_LIMIT
            ),
            *find_containment_candidates(
                conn,
                user_id,
                kind=probe_kind,
                probe=ident.display_name,
                exclude_id=ident.identity_id,
                limit=_CANDIDATE_LIMIT,
            ),
        ]
        for cand in neighbors:
            if cand.identity_id in own_ids:
                continue
            prev = by_id.get(cand.identity_id)
            if prev is None or cand.score > prev[0]:
                by_id[cand.identity_id] = (cand.score, cand.kind)
    if not by_id:
        return []
    rows = conn.execute(
        text(
            """
            SELECT i.id, i.kind, i.display_name, i.aliases,
                   p.display_name AS parent_name,
                   COALESCE(array_agg(f.value_norm) FILTER (WHERE f.kind = 'domain'), '{}')
                       AS domains
            FROM mod_identidades i
            LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id
            LEFT JOIN mod_identidades_identifiers f
                   ON f.identity_id = i.id AND f.user_id = i.user_id AND f.kind = 'domain'
            WHERE i.user_id = :uid AND i.id = ANY(:ids)
            GROUP BY i.id, i.kind, i.display_name, i.aliases, p.display_name
            """
        ),
        {"uid": user_id, "ids": list(by_id)},
    ).mappings()
    out = [
        Candidate(
            identity_id=int(r["id"]),
            kind=str(r["kind"]),
            display_name=str(r["display_name"]),
            aliases=tuple(str(a) for a in (r["aliases"] or ())),
            domains=tuple(str(d) for d in (r["domains"] or ())),
            parent_name=str(r["parent_name"]) if r["parent_name"] is not None else None,
            score=by_id[int(r["id"])][0],
        )
        for r in rows
    ]
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def build_email_context(conn: Connection, user_id: int, inbox_id: int) -> ResolverInput | None:
    """Arma la entrada del resolvedor para un correo, o None si no hay nada que disponer (skip)."""
    idents = _email_identities(conn, user_id, inbox_id)
    if not idents:
        return None
    if all(i.resolved_context for i in idents) and not _sender_email_unassociated(
        conn, user_id, idents
    ):
        return None
    payload = conn.execute(
        text("SELECT payload FROM inbox WHERE id = :id AND user_id = :uid"),
        {"id": inbox_id, "uid": user_id},
    ).scalar()
    if payload is None:
        return None
    subject = str(payload.get("subject") or "")
    body = render_payload(payload)[:_BODY_CHARS]
    candidates = _candidates_for(conn, user_id, idents)
    return ResolverInput(
        inbox_id=inbox_id,
        subject=subject,
        body=body,
        identities=tuple(idents),
        candidates=tuple(candidates),
    )
