"""Fallback OFF/DESCONECTADO: atribuir un DOMINIO huérfano a la identidad dueña.

Un dominio es un ATRIBUTO, no una identidad — el camino del remitente (`senders._org_for_domain`) ya
NO crea fichas nombradas por el dominio. Cuando el resolver/extractor no ataron un dominio a su org
real, esta función le pregunta al LLM cuál de las identidades EXISTENTES es la dueña y, si hay una
clara, le cuelga el dominio como atributo (de ahí en más el lookup procedimental ata los emails de
ese dominio al re-tejer el remitente).

NO se llama desde el pipeline (DESCONECTADA): se invoca a mano por CLI (`attribute-domain`) cuando
el resolver/extractor no fueron capaces. No adivina ni crea orgs: solo elige entre las identidades
que ya existen (crear la org por nombre lo hace el clasificador codex+web, aparte)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.llm import ChatMessage, LLMClient, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.identidades.fuzzy import find_fuzzy_candidates
from memex.modules.identidades.module import _insert_identifier
from memex.modules.identidades.normalize import is_freemail, norm_identifier
from memex.modules.identidades.resolve import KIND_ORG
from memex.modules.identidades.senders import weave_email_senders
from memex.modules.identidades.settings import get_settings

_log = get_logger("memex.modules.identidades.domain_attribution")

#: Tope de candidatas que ve el LLM (acota tokens; co-ocurrencia + fuzzy).
_CAND_CAP = 12

DOMAIN_OWNER_SYSTEM_PROMPT = (
    "Te doy un DOMINIO (o URL) y candidatas del directorio (con su alias y su `padre` en la "
    "jerarquía). Decidí si el dominio PERTENECE a una de ellas — su dominio institucional — y a "
    "CUÁL. Un dominio es un ATRIBUTO de UNA organización, no una entidad.\n"
    "REGLA: el dominio pertenece a la org de MÁS ALTO NIVEL (la institución, p. ej. la U.), NO a "
    "una sub-unidad/oficina. Si la dueña obvia es una sub-unidad, subí por su `padre` hasta la "
    "RAÍZ y atribuí a esa. Si ninguna calza, owner_id null (mejor no atar que atar mal).\n"
    'Respondé SOLO con un objeto JSON: {"owner_id": <id|null>, "confidence": <0..1>}.'
)


@dataclass(frozen=True)
class DomainAttribution:
    """A quién pertenece un dominio (si se decidió), y si se aplicó la atadura."""

    domain: str
    owner_id: int | None
    owner_name: str | None
    confidence: float
    candidates: int
    applied: bool


def _candidates(
    conn: Connection, user_id: int, domain: str
) -> list[tuple[int, str, tuple[str, ...], str | None]]:
    """Identidades-org que podrían ser dueñas del dominio: orgs que CO-OCURREN (aparecen en correos
    cuyo remitente es @domain) + fuzzy por la etiqueta. MÁS su JERARQUÍA (ancestros): así el LLM
    puede atribuir el dominio a la org RAÍZ (la universidad), no a una sub-unidad — igual que el
    contexto del resolver muestra el padre. Devuelve (id, nombre, alias, padre)."""
    direct = {
        int(x)
        for x in conn.execute(
            text(
                "SELECT DISTINCT i.id FROM inbox inb "
                "JOIN mod_identidades_mentions m ON inb.id = ANY(m.source_inbox_ids) "
                "JOIN mod_identidades i ON i.id = m.resolved_identity_id "
                "WHERE inb.user_id = :u AND i.user_id = :u AND i.kind = 'organizacion' "
                "AND split_part(lower(inb.payload->'from'->>'email'), '@', 2) = :d"
            ),
            {"u": user_id, "d": domain},
        ).scalars()
    }
    label = domain.split(".")[0]
    if label:
        fuzzy = find_fuzzy_candidates(conn, user_id, kind=KIND_ORG, probe=label, limit=_CAND_CAP)
        for cand in fuzzy:
            direct.add(cand.identity_id)
    direct = set(list(direct)[:_CAND_CAP])
    if not direct:
        return []
    # + ancestros (parte de la jerarquía) para OFRECER la raíz como candidata
    all_ids = {
        int(x)
        for x in conn.execute(
            text(
                "WITH RECURSIVE chain(id, pid) AS ("
                "  SELECT id, parent_identity_id FROM mod_identidades "
                "  WHERE user_id = :u AND id = ANY(:ids) "
                "  UNION "
                "  SELECT p.id, p.parent_identity_id FROM mod_identidades p "
                "  JOIN chain c ON p.id = c.pid WHERE p.user_id = :u"
                ") SELECT DISTINCT id FROM chain"
            ),
            {"u": user_id, "ids": list(direct)},
        ).scalars()
    }
    rows = conn.execute(
        text(
            "SELECT i.id, i.display_name, i.aliases, p.display_name AS parent_name "
            "FROM mod_identidades i LEFT JOIN mod_identidades p ON p.id = i.parent_identity_id "
            "WHERE i.user_id = :u AND i.id = ANY(:ids) AND i.kind = 'organizacion' ORDER BY i.id"
        ),
        {"u": user_id, "ids": list(all_ids)},
    ).mappings()
    return [
        (
            int(r["id"]),
            str(r["display_name"]),
            tuple(str(a) for a in (r["aliases"] or ())),
            str(r["parent_name"]) if r["parent_name"] is not None else None,
        )
        for r in rows
    ]


def _parse_owner(content: str, valid: set[int]) -> tuple[int | None, float]:
    """{owner_id, confidence} del LLM; owner_id debe ser una candidata válida, si no → None."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, 0.0
    if not isinstance(data, dict):
        return None, 0.0
    oid = data.get("owner_id")
    oid = oid if isinstance(oid, int) and not isinstance(oid, bool) and oid in valid else None
    raw = data.get("confidence")
    conf = 0.0
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        conf = max(0.0, min(1.0, float(raw)))
    return oid, conf


async def attribute_domain(
    conn: Connection,
    user_id: int,
    domain: str,
    *,
    llm: LLMClient | None = None,
    apply: bool = True,
    min_confidence: float = 0.7,
) -> DomainAttribution:
    """Pregunta al LLM si `domain` pertenece a alguna identidad existente y a cuál; si hay una clara
    (>= `min_confidence`) y `apply`, le cuelga el dominio + re-teje los remitentes de ese dominio
    (los emails se atan solos por lookup). Fallback explícito: el pipeline NO la llama."""
    dom = norm_identifier("domain", domain)
    if not dom:
        return DomainAttribution(domain, None, None, 0.0, 0, False)
    owned = conn.execute(
        text(
            "SELECT i.id, i.display_name FROM mod_identidades_identifiers f "
            "JOIN mod_identidades i ON i.id = f.identity_id "
            "WHERE f.user_id = :u AND f.kind = 'domain' AND f.value_norm = :d LIMIT 1"
        ),
        {"u": user_id, "d": dom},
    ).first()
    if owned is not None:  # ya es atributo de alguien → nada que decidir
        return DomainAttribution(dom, int(owned[0]), str(owned[1]), 1.0, 0, False)
    cands = _candidates(conn, user_id, dom)
    if not cands:
        return DomainAttribution(dom, None, None, 0.0, 0, False)
    listing = "\n".join(
        f"  id={cid} nombre={nm!r} alias={list(al)} padre={pn or '-'}" for cid, nm, al, pn in cands
    )
    msg = f"DOMINIO: {dom}\n\nCANDIDATAS (con su padre en la jerarquía):\n{listing}"
    client = llm or build_llm_client("identidades_resolve", user_id=user_id)
    res = await client.complete(
        [ChatMessage("system", DOMAIN_OWNER_SYSTEM_PROMPT), ChatMessage("user", msg)],
        response_format="json_object",
        temperature=0.0,
        max_tokens=200,
    )
    owner_id, conf = _parse_owner(res.content, {cid for cid, *_ in cands})
    owner_name = next((nm for cid, nm, *_ in cands if cid == owner_id), None)
    applied = False
    if owner_id is not None and conf >= min_confidence and apply:
        _insert_identifier(
            conn, user_id, owner_id, "domain", "domain", domain, dom, source="domain_attribution"
        )
        ids = [
            int(r[0])
            for r in conn.execute(
                text(
                    "SELECT id FROM inbox WHERE user_id = :u "
                    "AND split_part(lower(payload->'from'->>'email'), '@', 2) = :d"
                ),
                {"u": user_id, "d": dom},
            ).all()
        ]
        if ids:
            weave_email_senders(conn, user_id, ids)
        applied = True
        _log.info(
            "identidades.domain_attribution.applied", domain=dom, owner_id=owner_id, confidence=conf
        )
    return DomainAttribution(dom, owner_id, owner_name, conf, len(cands), applied)


async def attribute_domains_for_window(
    user_id: int, inbox_ids: Sequence[int], *, llm: LLMClient | None = None
) -> int:
    """CONECTA `attribute_domain` al pipeline: tras la resolución de una ventana, ata los DOMINIOS
    corporativos de sus remitentes que quedaron SIN dueña a la identidad que el LLM elija. Gate =
    `resolver_enabled` (corre junto al resolver); no-op si está apagado. best-effort por dominio:
    uno que falle no frena al resto. Lo dispara el orquestador después de `run_resolver_window`."""
    ids = list(inbox_ids)
    if not ids:
        return 0
    with connection() as conn:
        if not get_settings(conn, user_id).resolver_enabled:
            return 0
        rows = conn.execute(
            text(
                "SELECT DISTINCT split_part(lower(inb.payload->'from'->>'email'), '@', 2) AS dom "
                "FROM inbox inb WHERE inb.user_id = :u AND inb.id = ANY(:ids) "
                "AND inb.payload->'from'->>'email' IS NOT NULL"
            ),
            {"u": user_id, "ids": ids},
        ).scalars()
        domains = [d for d in rows if d and not is_freemail(d)]
    if not domains:
        return 0
    client = llm or build_llm_client("identidades_resolve", user_id=user_id)
    attached = 0
    try:
        for dom in domains:
            try:
                with connection() as conn:
                    res = await attribute_domain(conn, user_id, dom, llm=client)
                if res.applied:
                    attached += 1
            except Exception as e:  # un dominio que falla no frena la ventana
                _log.warning(
                    "identidades.domain_attribution.window_error", domain=dom, error=str(e)
                )
    finally:
        if llm is None:
            await aclose_llm(client)
    return attached
