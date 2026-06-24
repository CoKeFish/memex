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
from memex.modules.identidades.resolve import KIND_ORG, KIND_PERSONA
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


def _parse_relations(
    raw: object, valid: set[int], kinds: dict[int, str]
) -> tuple[tuple[Parent, ...], tuple[Affiliation, ...]]:
    """RELACIONES de pertenencia (forma unificada) ruteadas por KIND: `source` persona → AFILIACIÓN;
    `source` org/producto → JERARQUÍA. El `target` es SIEMPRE una org (por id, o por `target_name`
    para crearla, solo en jerarquía). El sistema deduce el tipo (no lo decide el LLM). Descarta lo
    incoherente (ids fuera de lista, target que no es org)."""
    if not isinstance(raw, list):
        return (), ()
    parents: list[Parent] = []
    affils: list[Affiliation] = []
    seen_child: set[int] = set()  # un hijo cuelga de un solo padre
    for it in raw:
        if not isinstance(it, dict):
            continue
        src = _as_int(it.get("source_id"))
        if src is None or src not in valid:
            continue
        skind = kinds.get(src)
        tid = _as_int(it.get("target_id"))
        target_is_org = (
            tid is not None and tid != src and tid in valid and kinds.get(tid) == "organizacion"
        )
        conf = _as_conf(it.get("confidence"))
        if skind == "persona":  # afiliación: persona → org EXISTENTE (con rol opcional)
            if target_is_org and tid is not None:
                role = it.get("role")
                affils.append(Affiliation(src, tid, str(role) if role else None, conf))
        elif skind in ("organizacion", "producto") and src not in seen_child:  # jerarquía
            raw_name = it.get("target_name")
            tname = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
            if target_is_org:
                seen_child.add(src)
                parents.append(Parent(src, tid, None, conf))
            elif tname:  # org target por nombre (no está en las listas) → la crea el apply
                seen_child.add(src)
                parents.append(Parent(src, None, tname, conf))
    return tuple(parents), tuple(affils)


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


def parse_resolution(
    content: str, valid_ids_set: set[int], kinds: dict[int, str]
) -> ResolverDecision:
    """Parsea la respuesta del LLM; cualquier cosa rara → decisión vacía (no hace nada). Las
    RELACIONES (forma unificada) se rutean por `kinds` a jerarquía/afiliación.

    Lo que el LLM propuso y el parser descartó (ids fuera de lista, target que no es org…) se
    emite como WARNING (`parse_dropped`); el crudo queda en `llm_calls.response_text`."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return ResolverDecision((), (), None)
    if not isinstance(data, dict):
        return ResolverDecision((), (), None)
    parents, affiliations = _parse_relations(data.get("relations"), valid_ids_set, kinds)
    decision = ResolverDecision(
        merges=_parse_merges(data.get("merges"), valid_ids_set),
        parents=parents,
        sender=_parse_sender(data.get("sender"), valid_ids_set),
        affiliations=affiliations,
    )
    dropped_m = _dropped(data.get("merges"), decision.merges)
    dropped_r = _dropped(data.get("relations"), parents + affiliations)
    if dropped_m or dropped_r:
        _log.warning(
            "identidades.resolver.parse_dropped",
            dropped_merges=dropped_m,
            dropped_relations=dropped_r,
        )
    return decision


# --- llamada LLM ------------------------------------------------------------------- #


def _serialize(ctx: ResolverInput) -> str:
    # Las 3 partes del correo: REMITENTE (email) + ASUNTO + CUERPO. El email del remitente va aunque
    # quede sin atar (no es mención): su dominio dice de qué org viene.
    lines = [f"REMITENTE DEL CORREO: {ctx.sender_email}"] if ctx.sender_email else []
    lines += [f"ASUNTO: {ctx.subject}", f"CUERPO: {ctx.body}", "", "IDENTIDADES DEL CORREO:"]
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
    # kinds (id→tipo) para rutear las RELACIONES: persona→org afiliación; org→org jerarquía.
    kinds = {i.identity_id: i.kind for i in ctx.identities}
    kinds.update({c.identity_id: c.kind for c in ctx.candidates})
    return parse_resolution(result.content, valid_ids(ctx), kinds), result


# --- aplicación -------------------------------------------------------------------- #


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


def _upsert_sender_mention(
    conn: Connection, user_id: int, inbox_id: int, identity_id: int, kind: str, email: str
) -> None:
    """Re-apunta la mención 'sender' del correo a `identity_id` (la decisión del LLM); si no hay (el
    remitente corporativo llega LEFTOVER), la inserta. Se hace POST-resolución → el remitente
    co-ocurre sin haber sido FUENTE de la jerarquía (era lo que la invertía)."""
    from memex.modules.identidades.senders import _insert_sender_mention, _mentioned_kind

    mk = _mentioned_kind(kind)
    updated = conn.execute(
        text(
            "UPDATE mod_identidades_mentions SET resolved_identity_id = :i, resolved_kind = :k, "
            "mentioned_kind = :mk WHERE user_id = :u AND :inbox = ANY(source_inbox_ids) "
            "AND resolution_method = 'sender'"
        ),
        {"i": identity_id, "k": kind, "mk": mk, "u": user_id, "inbox": inbox_id},
    ).rowcount
    if not updated:
        _insert_sender_mention(
            conn,
            user_id,
            inbox_id,
            identity_id=identity_id,
            resolved_kind=kind,
            name=email,
            mentioned_kind=mk,
            email=email,
        )


def _dispose_sender(
    conn: Connection, user_id: int, ctx: ResolverInput, sd: SenderDisposition, min_person: float
) -> ResolverStats:
    """Dispone el email del remitente según la DECISIÓN del LLM (no por dominio): persona (conf
    alta) → ficha propia + email + afiliada a la org elegida; buzón → email como contacto de la
    org ELEGIDA (`owner_id`). El remitente corporativo llega LEFTOVER (el procedimental ya no lo
    resuelve por dominio); su lugar lo decide acá el LLM y se (re)graba su mención para co-ocurrir.
    Si el LLM no eligió dueña viva → leftover (no se inventa nada)."""
    stats = ResolverStats()
    email = ctx.sender_email
    if not email:
        return stats
    vn = norm_identifier("email", email)
    if not vn:
        return stats
    # La org elegida por el LLM (`owner_id`) puede venir nula o haber sido fundida/muerta por los
    # merges de este mismo apply → solo se usa si está VIVA (su kind sirve de check de vida).
    owner: int | None = None
    owner_kind = KIND_ORG
    if sd.owner_id is not None:
        alive = _kinds(conn, user_id, {sd.owner_id})
        if sd.owner_id in alive:
            owner, owner_kind = sd.owner_id, alive[sd.owner_id]
    if sd.is_person and sd.person_name and sd.confidence >= min_person:
        person_id = _resolve_or_create_person(conn, user_id, sd.person_name)
        _insert_identifier(conn, user_id, person_id, "email", "email", email, vn, source=_SOURCE)
        if owner is not None:
            _affiliate(conn, user_id, person_id, owner, None, source=_SOURCE)
        _upsert_sender_mention(conn, user_id, ctx.inbox_id, person_id, KIND_PERSONA, email)
        stats.persons += 1
    elif owner is not None:  # buzón de la org ELEGIDA por el LLM
        _insert_identifier(conn, user_id, owner, "email", "email", email, vn, source=_SOURCE)
        _upsert_sender_mention(conn, user_id, ctx.inbox_id, owner, owner_kind, email)
        stats.contacts += 1
    # else: el LLM no eligió dueña viva → leftover (el email queda sin atar; nada que inventar)
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
