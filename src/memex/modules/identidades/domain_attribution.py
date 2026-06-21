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
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.llm import ChatMessage, LLMClient, build_llm_client
from memex.logging import get_logger
from memex.modules.identidades.fuzzy import find_fuzzy_candidates
from memex.modules.identidades.module import _insert_identifier
from memex.modules.identidades.normalize import norm_identifier
from memex.modules.identidades.resolve import KIND_ORG
from memex.modules.identidades.senders import weave_email_senders

_log = get_logger("memex.modules.identidades.domain_attribution")

#: Tope de candidatas que ve el LLM (acota tokens; co-ocurrencia + fuzzy).
_CAND_CAP = 12

DOMAIN_OWNER_SYSTEM_PROMPT = (
    "Te doy un DOMINIO (o URL) y una lista de IDENTIDADES candidatas del directorio (con alias).\n"
    "Decidí si el dominio PERTENECE a una de ellas — si es su dominio institucional — y a CUÁL.\n"
    "Un dominio es un ATRIBUTO de UNA organización, no una entidad. Si ninguna es claramente la "
    "dueña, devolvé owner_id null (mejor no atar que atar mal).\n"
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
) -> list[tuple[int, str, tuple[str, ...]]]:
    """Identidades-org que podrían ser dueñas del dominio: orgs que CO-OCURREN (aparecen en correos
    cuyo remitente es @domain) + fuzzy por la etiqueta del dominio. Dedup por id, acotado."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT i.id, i.display_name, i.aliases "
            "FROM inbox inb "
            "JOIN mod_identidades_mentions m ON inb.id = ANY(m.source_inbox_ids) "
            "JOIN mod_identidades i ON i.id = m.resolved_identity_id "
            "WHERE inb.user_id = :u AND i.user_id = :u AND i.kind = 'organizacion' "
            "AND split_part(lower(inb.payload->'from'->>'email'), '@', 2) = :d"
        ),
        {"u": user_id, "d": domain},
    ).all()
    by_id: dict[int, tuple[str, tuple[str, ...]]] = {
        int(r[0]): (str(r[1]), tuple(str(a) for a in (r[2] or ()))) for r in rows
    }
    label = domain.split(".")[0]
    if label:
        fuzzy = find_fuzzy_candidates(conn, user_id, kind=KIND_ORG, probe=label, limit=_CAND_CAP)
        for cand in fuzzy:
            by_id.setdefault(cand.identity_id, (cand.display_name, ()))
    return [(cid, nm, al) for cid, (nm, al) in list(by_id.items())[:_CAND_CAP]]


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
    listing = "\n".join(f"  id={cid} nombre={nm!r} alias={list(al)}" for cid, nm, al in cands)
    msg = f"DOMINIO: {dom}\n\nCANDIDATAS:\n{listing}"
    client = llm or build_llm_client("identidades_resolve", user_id=user_id)
    res = await client.complete(
        [ChatMessage("system", DOMAIN_OWNER_SYSTEM_PROMPT), ChatMessage("user", msg)],
        response_format="json_object",
        temperature=0.0,
        max_tokens=200,
    )
    owner_id, conf = _parse_owner(res.content, {cid for cid, _, _ in cands})
    owner_name = next((nm for cid, nm, _ in cands if cid == owner_id), None)
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
