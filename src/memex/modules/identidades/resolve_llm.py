"""Resolvedor contextual por-correo: la llamada LLM (fusión + jerarquía + disposición del
remitente) y su aplicación, reusando los primitivos de identidades.

Molde de `dedup_llm.disambiguate_pair`: system + user, temp 0, `json_object`, parseo
ULTRA-DEFENSIVO (ids fuera de las listas, XOR de jerarquía, sesgo a coexistir/precisión). El
`apply` corre sobre el `conn` de la persistencia (atómico) y reusa: `merge_identities` (funde +
alias + anti-ciclo), `_apply_links` (pinned/ciclo/_set_parent) y `_insert_identifier`/`_affiliate`
(contacto del remitente). Marca `metadata.resolved_context_at` para no re-correr.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.identidades.hierarchy import HierarchyLink, _apply_links
from memex.modules.identidades.merge import merge_identities
from memex.modules.identidades.module import _affiliate, _insert_identifier
from memex.modules.identidades.normalize import norm_identifier
from memex.modules.identidades.prompt import IDENTIDADES_RESOLVE_SYSTEM_PROMPT
from memex.modules.identidades.resolve_context import (
    ResolverInput,
    build_email_context,
    email_needs_resolution,
)
from memex.modules.identidades.settings import get_settings

_MAX_TOKENS = 1024
_SOURCE = "extraction"  # valor permitido por el CHECK de `source` en identifiers/afiliación

_log = get_logger("memex.modules.identidades.resolve_llm")


@dataclass(frozen=True)
class Merge:
    survivor_id: int
    absorbed_id: int
    confidence: float


@dataclass(frozen=True)
class Parent:
    child_id: int
    parent_id: int | None
    parent_name: str | None
    confidence: float


@dataclass(frozen=True)
class SenderDisposition:
    """Qué es el email del remitente: una persona (ficha propia) o un buzón de una org."""

    is_person: bool
    owner_id: int | None  # buzón: la org dueña (None = la org provisional del remitente)
    person_name: str | None  # persona: su nombre
    confidence: float


@dataclass(frozen=True)
class Affiliation:
    """Una PERSONA del correo es miembro de una ORGANIZACIÓN, con rol. Distinto de jerarquía:
    persona→org es AFILIACIÓN (`person_orgs`), no «pertenece_a» (que es solo org/producto)."""

    person_id: int
    org_id: int
    role: str | None
    confidence: float


@dataclass(frozen=True)
class ResolverDecision:
    merges: tuple[Merge, ...]
    parents: tuple[Parent, ...]
    sender: SenderDisposition | None
    affiliations: tuple[Affiliation, ...] = ()


@dataclass
class ResolverStats:
    merged: int = 0
    linked: int = 0
    contacts: int = 0  # emails de remitente atados a una org (buzón)
    persons: int = 0  # personas del remitente creadas/resueltas
    affiliated: int = 0  # personas del CUERPO afiliadas a su org vía org_hint (no remitentes)
    errors: int = 0


# --- parseo (ultra-defensivo) ------------------------------------------------------ #


def _as_int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _as_conf(v: object) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return max(0.0, min(1.0, float(v)))
    return 0.0


def _parse_merges(raw: object, valid: set[int]) -> tuple[Merge, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[Merge] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        keep, drop = _as_int(it.get("keep_id")), _as_int(it.get("drop_id"))
        if keep is None or drop is None or keep == drop:
            continue
        if keep not in valid or drop not in valid:
            continue
        conf = _as_conf(it.get("confidence"))
        out.append(Merge(survivor_id=keep, absorbed_id=drop, confidence=conf))
    return tuple(out)


def _parse_parents(raw: object, valid: set[int]) -> tuple[Parent, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[Parent] = []
    seen: set[int] = set()
    for it in raw:
        if not isinstance(it, dict):
            continue
        child = _as_int(it.get("child_id"))
        if child is None or child not in valid or child in seen:
            continue
        pid = _as_int(it.get("parent_id"))
        if pid is not None and (pid not in valid or pid == child):
            pid = None  # id inválido/alucinado/self → caé al parent_name si lo hay
        raw_name = it.get("parent_name")
        pname = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        # El LLM suele mandar AMBOS (parent_id + parent_name); `_apply_links` prefiere el id. Solo
        # se descarta si NO quedó ninguno válido (antes un XOR tiraba todo lo que traía ambos).
        if pid is None and pname is None:
            continue
        seen.add(child)
        conf = _as_conf(it.get("confidence"))
        out.append(Parent(child_id=child, parent_id=pid, parent_name=pname, confidence=conf))
    return tuple(out)


def _parse_sender(raw: object, valid: set[int]) -> SenderDisposition | None:
    if not isinstance(raw, dict):
        return None
    is_person = raw.get("is_person")
    if not isinstance(is_person, bool):
        return None
    conf = _as_conf(raw.get("confidence"))
    if is_person:
        name = raw.get("person_name")
        if not (isinstance(name, str) and name.strip()):
            return None
        return SenderDisposition(
            is_person=True, owner_id=None, person_name=name.strip(), confidence=conf
        )
    owner = _as_int(raw.get("owner_id"))
    return SenderDisposition(
        is_person=False,
        owner_id=owner if owner in valid else None,
        person_name=None,
        confidence=conf,
    )


def _dropped(raw: object, kept: tuple[object, ...]) -> int:
    """Items que el LLM propuso (lista) y el parser NO conservó (id inválido/fuera de lista…)."""
    return max((len(raw) if isinstance(raw, list) else 0) - len(kept), 0)


def _parse_affiliations(raw: object, valid: set[int]) -> tuple[Affiliation, ...]:
    """Afiliaciones persona→org del LLM. Descarta las que referencian ids fuera de las listas o
    self-afiliación; el kind (persona→organizacion) se valida al aplicar (sobre ids vivos)."""
    if not isinstance(raw, list):
        return ()
    out: list[Affiliation] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        pid = _as_int(a.get("person_id"))
        oid = _as_int(a.get("org_id"))
        if pid is None or oid is None or pid == oid or pid not in valid or oid not in valid:
            continue
        role = a.get("role")
        conf = _as_conf(a.get("confidence"))
        out.append(Affiliation(pid, oid, str(role) if role else None, conf))
    return tuple(out)


def parse_resolution(content: str, valid_ids_set: set[int]) -> ResolverDecision:
    """Parsea la respuesta del LLM; cualquier cosa rara → decisión vacía (no hace nada).

    Si el LLM propuso fusiones/jerarquías que el parser descartó, emite un WARNING
    (`parse_dropped`) — sin esto los drops eran SILENCIOSOS y escondieron el bug del XOR de
    parents. El detalle crudo queda en `llm_calls.response_text` para inspeccionar."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return ResolverDecision((), (), None)
    if not isinstance(data, dict):
        return ResolverDecision((), (), None)
    decision = ResolverDecision(
        merges=_parse_merges(data.get("merges"), valid_ids_set),
        parents=_parse_parents(data.get("parents"), valid_ids_set),
        sender=_parse_sender(data.get("sender"), valid_ids_set),
        affiliations=_parse_affiliations(data.get("affiliations"), valid_ids_set),
    )
    dropped_m = _dropped(data.get("merges"), decision.merges)
    dropped_p = _dropped(data.get("parents"), decision.parents)
    dropped_a = _dropped(data.get("affiliations"), decision.affiliations)
    if dropped_m or dropped_p or dropped_a:
        _log.warning(
            "identidades.resolver.parse_dropped",
            dropped_merges=dropped_m,
            dropped_parents=dropped_p,
            dropped_affiliations=dropped_a,
        )
    return decision


# --- llamada LLM ------------------------------------------------------------------- #


def _serialize(ctx: ResolverInput) -> str:
    lines = [f"ASUNTO: {ctx.subject}", f"CUERPO: {ctx.body}", "", "IDENTIDADES DEL CORREO:"]
    for i in ctx.identities:
        mark = f"  [REMITENTE: {i.sender_email}]" if i.is_sender and i.sender_email else ""
        datos = f" datos=[{', '.join(i.identifiers)}]" if i.identifiers else ""
        padre = f" padre={i.parent_name!r}" if i.parent_name else ""
        hijos = f" hijos=[{', '.join(i.children)}]" if i.children else ""
        base = f"  id={i.identity_id} tipo={i.kind} nombre={i.display_name!r}"
        lines.append(f"{base}{datos}{padre}{hijos}{mark}")
    lines += ["", "CANDIDATAS DEL DIRECTORIO:"]
    if not ctx.candidates:
        lines.append("  (ninguna)")
    for c in ctx.candidates:
        al = ",".join(c.aliases) or "-"
        dom = ",".join(c.domains) or "-"
        lines.append(
            f"  id={c.identity_id} tipo={c.kind} nombre={c.display_name!r} "
            f"alias=[{al}] dominios=[{dom}] padre={c.parent_name or '-'}"
        )
    return "\n".join(lines)


def valid_ids(ctx: ResolverInput) -> set[int]:
    """Ids que el LLM puede referenciar: las del correo + las candidatas."""
    return {i.identity_id for i in ctx.identities} | {c.identity_id for c in ctx.candidates}


async def resolve_email(llm: LLMClient, ctx: ResolverInput) -> tuple[ResolverDecision, LLMResult]:
    """Una llamada LLM con el contexto del correo → fusión/jerarquía/disposición del remitente."""
    result = await llm.complete(
        [
            ChatMessage("system", IDENTIDADES_RESOLVE_SYSTEM_PROMPT),
            ChatMessage("user", _serialize(ctx)),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return parse_resolution(result.content, valid_ids(ctx)), result


# --- aplicación -------------------------------------------------------------------- #


def _alive(conn: Connection, user_id: int, ids: set[int]) -> set[int]:
    if not ids:
        return set()
    rows = conn.execute(
        text("SELECT id FROM mod_identidades WHERE user_id = :u AND id = ANY(:ids)"),
        {"u": user_id, "ids": list(ids)},
    ).all()
    return {int(r[0]) for r in rows}


def _kinds(conn: Connection, user_id: int, ids: set[int]) -> dict[int, str]:
    """{id → kind} de los ids VIVOS (los muertos/fundidos quedan fuera → sirve de check de vida)."""
    if not ids:
        return {}
    rows = conn.execute(
        text("SELECT id, kind FROM mod_identidades WHERE user_id = :u AND id = ANY(:ids)"),
        {"u": user_id, "ids": list(ids)},
    )
    return {int(r[0]): str(r[1]) for r in rows}


def _resolve_or_create_person(conn: Connection, user_id: int, name: str) -> int:
    """Persona existente por nombre normalizado (`memex_norm`, como la columna generada) o nueva."""
    existing = conn.execute(
        text(
            "SELECT id FROM mod_identidades WHERE user_id = :u AND kind = 'persona' "
            "AND memex_norm(:n) <> '' AND name_norm = memex_norm(:n) ORDER BY id LIMIT 1"
        ),
        {"u": user_id, "n": name},
    ).scalar()
    if existing is not None:
        return int(existing)
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, source, metadata) "
                "VALUES (:u, 'persona', :n, 'extraction', "
                "jsonb_build_object('created_by', 'resolver')) RETURNING id"
            ),
            {"u": user_id, "n": name},
        ).scalar_one()
    )


def _dispose_sender(
    conn: Connection, user_id: int, ctx: ResolverInput, sd: SenderDisposition, min_person: float
) -> ResolverStats:
    """Dispone el email del remitente: buzón → contacto de la org; persona (conf alta) → ficha
    propia con el email + afiliada a la org + re-apunta la mención. Sin remitente → no-op."""
    stats = ResolverStats()
    sender = next((i for i in ctx.identities if i.is_sender and i.sender_email), None)
    if sender is None or not sender.sender_email:
        return stats
    email = sender.sender_email
    vn = norm_identifier("email", email)
    if not vn:
        return stats
    # La org del remitente pudo ser FUNDIDA por los merges previos de este mismo apply → su id de
    # `ctx` quedaría colgante (FK violation). Se RE-LEE del mention VIVO (merge re-apunta las
    # menciones al superviviente); sin org viva, se omite (el email ya lo colgó el camino
    # procedimental del remitente).
    org_id = conn.execute(
        text(
            "SELECT resolved_identity_id FROM mod_identidades_mentions "
            "WHERE user_id = :u AND :inbox = ANY(source_inbox_ids) "
            "AND resolution_method = 'sender' AND resolved_identity_id IS NOT NULL LIMIT 1"
        ),
        {"u": user_id, "inbox": ctx.inbox_id},
    ).scalar()
    if org_id is None:
        return stats
    org_id = int(org_id)
    if sd.is_person and sd.person_name and sd.confidence >= min_person:
        person_id = _resolve_or_create_person(conn, user_id, sd.person_name)
        _insert_identifier(conn, user_id, person_id, "email", "email", email, vn, source=_SOURCE)
        _affiliate(conn, user_id, person_id, org_id, None, source=_SOURCE)
        conn.execute(
            text(
                "UPDATE mod_identidades_mentions SET resolved_identity_id = :p, "
                "resolved_kind = 'persona' WHERE user_id = :u AND :inbox = ANY(source_inbox_ids) "
                "AND resolution_method = 'sender'"
            ),
            {"p": person_id, "u": user_id, "inbox": ctx.inbox_id},
        )
        stats.persons += 1
    else:  # buzón de la org (o conf baja → default seguro: el email pertenece al dominio)
        # `owner` del LLM puede estar fundido/muerto → cae a la org viva del mention.
        owner = org_id
        if sd.owner_id is not None and _alive(conn, user_id, {sd.owner_id}):
            owner = sd.owner_id
        _insert_identifier(conn, user_id, owner, "email", "email", email, vn, source=_SOURCE)
        stats.contacts += 1
    return stats


def _mark_resolved(conn: Connection, user_id: int, inbox_id: int) -> None:
    """Marca `resolved_context_at` en las identidades vivas del correo (skip de la próxima vez)."""
    conn.execute(
        text(
            "UPDATE mod_identidades SET "
            "metadata = jsonb_set(metadata, '{resolved_context_at}', to_jsonb(NOW()), true), "
            "updated_at = NOW() "
            "WHERE user_id = :u AND id IN ("
            "  SELECT DISTINCT resolved_identity_id FROM mod_identidades_mentions "
            "  WHERE user_id = :u AND :inbox = ANY(source_inbox_ids) "
            "    AND resolved_identity_id IS NOT NULL)"
        ),
        {"u": user_id, "inbox": inbox_id},
    )


def apply_resolution(
    conn: Connection,
    user_id: int,
    ctx: ResolverInput,
    decision: ResolverDecision,
    *,
    min_merge: float,
    min_parent: float,
) -> ResolverStats:
    """Aplica la decisión sobre `conn`. Orden: fusiones → jerarquía → remitente → afiliaciones →
    marca. Todo sobre ids VIVOS tras las fusiones (evita FK a un id absorbido)."""
    stats = ResolverStats()
    for m in decision.merges:
        if m.confidence < min_merge:
            continue
        if merge_identities(conn, user_id, m.survivor_id, m.absorbed_id):
            stats.merged += 1
    # Jerarquía: SOLO org/producto cuelgan («pertenece_a»); una PERSONA nunca (su vínculo con la org
    # es una afiliación, abajo). Sobre ids vivos tras las fusiones.
    kept = [p for p in decision.parents if p.confidence >= min_parent]
    ref_kinds = _kinds(
        conn, user_id, {p.child_id for p in kept} | {p.parent_id for p in kept if p.parent_id}
    )
    links = [
        HierarchyLink(p.child_id, p.parent_id, p.parent_name, None)
        for p in kept
        if ref_kinds.get(p.child_id) in ("organizacion", "producto")
        and (p.parent_id is None or p.parent_id in ref_kinds)
    ]
    if links:
        linked, *_ = _apply_links(conn, user_id, links, apply_cleanup=False)
        stats.linked += linked
    if decision.sender is not None:
        sender_stats = _dispose_sender(conn, user_id, ctx, decision.sender, min_merge)
        stats.contacts += sender_stats.contacts
        stats.persons += sender_stats.persons
    # Afiliaciones persona→org que decidió el LLM (mapea la org por contexto aunque el nombre no
    # calce exacto; reemplaza el viejo match exacto). Valida kind (persona→organizacion) + vida.
    af_kinds = _kinds(
        conn,
        user_id,
        {a.person_id for a in decision.affiliations} | {a.org_id for a in decision.affiliations},
    )
    for a in decision.affiliations:
        if a.confidence < min_parent:
            continue
        if af_kinds.get(a.person_id) == "persona" and af_kinds.get(a.org_id) == "organizacion":
            _affiliate(conn, user_id, a.person_id, a.org_id, a.role, source=_SOURCE)
            stats.affiliated += 1
    _mark_resolved(conn, user_id, ctx.inbox_id)
    return stats


async def run_resolver_window(
    user_id: int, inbox_ids: Sequence[int], *, client: LLMClient | None = None
) -> ResolverStats:
    """Corre el resolvedor en los correos de la ventana que lo necesiten (skip incremental), con
    tope de llamadas. Gate apagado → no-op. best-effort por correo; cada llamada va a `llm_calls`
    (`purpose='identidades_resolve'`). `client` inyectable (tests con fake)."""
    stats = ResolverStats()
    with connection() as conn:
        settings = get_settings(conn, user_id)
        if not settings.resolver_enabled:
            return stats
        pending = [i for i in inbox_ids if email_needs_resolution(conn, user_id, i)]
    owns = client is None
    llm = client or build_llm_client("identidades_resolve", user_id=user_id)
    calls = 0
    try:
        for iid in pending:
            if calls >= settings.max_calls_per_window:
                break
            with connection() as conn:
                ctx = build_email_context(conn, user_id, iid)
            if ctx is None:
                continue
            try:
                decision, result = await resolve_email(llm, ctx)
            except Exception as e:  # best-effort: un correo no frena la ventana
                stats.errors += 1
                _log.error(
                    "identidades.resolver.email_failed",
                    inbox_id=iid,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            calls += 1
            with connection() as conn:
                s = apply_resolution(
                    conn,
                    user_id,
                    ctx,
                    decision,
                    min_merge=settings.min_confidence_merge,
                    min_parent=settings.min_confidence_parent,
                )
            stats.merged += s.merged
            stats.linked += s.linked
            stats.contacts += s.contacts
            stats.persons += s.persons
            stats.affiliated += s.affiliated
            record_llm_call(
                user_id=user_id,
                purpose="identidades_resolve",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                response_text=result.content,
                inbox_id=iid,
                metadata={
                    "merged": s.merged,
                    "linked": s.linked,
                    "contacts": s.contacts,
                    "persons": s.persons,
                    "affiliated": s.affiliated,
                },
            )
    finally:
        if owns:
            await aclose_llm(llm)
    _log.info(
        "identidades.resolver.window_done",
        user_id=user_id,
        calls=calls,
        merged=stats.merged,
        linked=stats.linked,
        contacts=stats.contacts,
        persons=stats.persons,
        affiliated=stats.affiliated,
        errors=stats.errors,
    )
    return stats
