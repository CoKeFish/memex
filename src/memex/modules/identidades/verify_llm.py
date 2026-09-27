"""Higiene del directorio tras la extracción: re-resolución determinista + segunda verificación.

Dos piezas, en este orden (ADR-015 §4; regla del dueño 2026-09-07: DeepSeek para el volumen,
codex para arbitrar):

1. **Re-resolución determinista** (`reresolve_unresolved`, sin LLM). Una mención queda «sin
   identidad» si al persistirla ninguna señal fuerte coincidía; pero el directorio cambia
   después (el resolvedor contextual agrega el email del remitente a su ficha, se crea la ficha,
   se funde otra). Caso real: 4 menciones del dueño con su correo institucional quedaron sueltas
   porque ese email entró a su ficha 16 minutos más tarde. Re-correr `KnownIndex.resolve` sobre
   las sueltas con el directorio ACTUAL las ata por email/handle/nombre exacto/alias. Barato
   (todo en memoria) e idempotente.

2. **Segunda verificación** (`run_verify`, juez codex). Reglas baratas marcan fichas dudosas —
   persona con nombre de UNA palabra («Ant»), ficha cuyo nombre es PREFIJO de otra del mismo
   tipo («Ant»/«Ant Wilson», «Amazon»/«Amazon Web Services»), organización/producto sin
   identificadores y nombre de ≤3 letras («X»)— y solo esas van a un juez que ve los correos de
   origen y las fichas candidatas. Veredictos: `attach` (es la candidata → `merge_identities`),
   `separate` (ficha legítima), `drop` (fragmento/remitente automático → la ficha desaparece y
   sus menciones vuelven a «sin identidad»). Cada ficha juzgada queda marcada
   (`metadata.verified_at`) → incremental. Experimento 2026-09-07 (16 casos): el juez acertó los
   16 y corrigió tres supuestos humanos («Erik» no era «Erika»; «X» es X Corp; AWS bajo Amazon es
   jerarquía, no duplicado).

`run_hygiene_window` las encadena tras el resolvedor contextual de cada ventana (solo para lo que
esa ventana tocó); los CLIs `reresolve` / `verify` cubren el histórico.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostAccum, record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.identidades.merge import merge_identities
from memex.modules.identidades.module import load_known_index
from memex.modules.identidades.resolve import KnownIndex
from memex.modules.identidades.schema import IdentityItem
from memex.processing.render import render_payload
from memex.relations.edges import Ref
from memex.relations.graph_writer import delete_vertex
from memex.relations.vertices import IDENTITY_SLUG_BY_KIND

_log = get_logger("memex.modules.identidades.verify")

PURPOSE = "identidades_verify"
#: Consumidor del registro LLM que sirve al juez (arbitraje → codex, decisión del dueño).
CONSUMER = "identidades_dedup"
#: Confianza mínima para APLICAR un `attach` o un `drop`; por debajo se conserva (`separate`).
MIN_CONFIDENCE = 0.7
_MAX_TOKENS = 300
_BODY_CHARS = 1200
_MAX_MESSAGES = 2
_MAX_CANDIDATES = 4
#: Nombre de organización/producto de hasta esta longitud sin identificadores → dudoso.
SHORT_ORG_NAME = 3

VERIFY_SYSTEM_PROMPT = (
    "Sos el curador de un directorio PERSONAL de identidades (personas, organizaciones y\n"
    "productos) que se arma automáticamente a partir de los correos de una persona (el DUEÑO).\n"
    "Te paso UNA ficha dudosa, con el texto de los correos donde apareció y las fichas\n"
    "candidatas del directorio (con sus identificadores y cuántas menciones tienen). Decidí UNA\n"
    "cosa:\n"
    "- 'attach': es la misma entidad que una candidata (indicá `target_id`). Ej.: la firma «Ant»\n"
    "  en un correo del remitente ant.wilson@… y la ficha «Ant Wilson»; «BELLA PIEL» y «BELLA\n"
    "  PIEL SAS».\n"
    "- 'separate': es una entidad distinta y merece ficha propia (una división o programa de una\n"
    "  organización es 'separate': eso es jerarquía, no duplicado).\n"
    "- 'drop': no es una entidad real o no merece ficha (un fragmento, un nombre genérico, el\n"
    "  nombre visible de un remitente automático como «Catherine at Notion», una ficha sin\n"
    "  ningún correo que la respalde Y sin nada que cuelgue de ella). Una ficha con `children`\n"
    "  o `links` > 0 sostiene la jerarquía o pagos aunque nadie la mencione: NO es 'drop'.\n"
    "Sé conservador con 'attach': solo si el correo lo respalda (mismo remitente, mismo dominio,\n"
    "mismo contexto). Respondé SOLO JSON:\n"
    '{"verdict": "<attach|separate|drop>", "target_id": <int|null>, '
    '"confidence": <0.0-1.0>, "rationale": "<motivo breve>"}'
)


# --- 1. re-resolución determinista ---------------------------------------------------- #


@dataclass
class ReresolveStats:
    scanned: int = 0
    resolved: int = 0
    by_method: dict[str, int] = field(default_factory=dict)


def _kind_ok(mention_kind: str, resolved_kind: str | None) -> bool:
    """Una mención tipada no se ata a una ficha de OTRO grupo (persona ∥ org+producto)."""
    if resolved_kind is None or mention_kind == "unknown":
        return True
    persona = {"persona"}
    return (mention_kind in persona) == (resolved_kind in persona)


def reresolve_unresolved(
    conn: Connection,
    user_id: int,
    *,
    inbox_ids: Sequence[int] | None = None,
    limit: int = 500,
    index: KnownIndex | None = None,
) -> ReresolveStats:
    """Re-corre las señales FUERTES sobre las menciones sin identidad con el directorio actual.
    Sin LLM. `inbox_ids` acota a los mensajes de una ventana; None = todas (tope `limit`)."""
    stats = ReresolveStats()
    scope = "" if inbox_ids is None else " AND source_inbox_ids && CAST(:ids AS BIGINT[])"
    params: dict[str, Any] = {"u": user_id, "lim": limit}
    if inbox_ids is not None:
        params["ids"] = list(inbox_ids)
    rows = (
        conn.execute(
            text(
                f"""
                SELECT id, mentioned_name, mentioned_kind, email, handle, org_hint, role_hint
                FROM mod_identidades_mentions
                WHERE user_id = :u AND resolved_identity_id IS NULL{scope}
                ORDER BY id LIMIT :lim
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    if not rows:
        return stats
    idx = index or load_known_index(conn, user_id)
    for r in rows:
        stats.scanned += 1
        item = IdentityItem(
            source_inbox_ids=(0,),
            name=str(r["mentioned_name"]),
            kind=str(r["mentioned_kind"] or "unknown"),
            email=r["email"],
            handle=r["handle"],
            org=r["org_hint"],
            role=r["role_hint"],
        )
        res = idx.resolve(item)
        if res.identity_id is None or not _kind_ok(item.kind, res.kind):
            continue
        conn.execute(
            text(
                "UPDATE mod_identidades_mentions SET resolved_identity_id = :rid, "
                "resolved_kind = :rk, resolution_method = :m WHERE id = :id"
            ),
            {"rid": res.identity_id, "rk": res.kind, "m": res.method, "id": int(r["id"])},
        )
        stats.resolved += 1
        stats.by_method[res.method] = stats.by_method.get(res.method, 0) + 1
    if stats.resolved:
        _log.info(
            "identidades.reresolve.done",
            user_id=user_id,
            scanned=stats.scanned,
            resolved=stats.resolved,
            by_method=stats.by_method,
        )
    return stats


# --- 2. segunda verificación (juez) ---------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    """Una ficha dudosa: por qué se marcó, sus candidatas y los correos donde apareció."""

    rule: str
    identity: dict[str, Any]
    candidates: list[dict[str, Any]]
    messages: list[str]

    @property
    def identity_id(self) -> int:
        return int(self.identity["id"])


@dataclass(frozen=True)
class Verdict:
    verdict: str  # attach | separate | drop
    target_id: int | None
    confidence: float
    rationale: str


@dataclass
class VerifyStats:
    cases: int = 0
    attached: int = 0
    dropped: int = 0
    kept: int = 0
    errors: int = 0
    cost: CostAccum = field(default_factory=CostAccum)


def _ficha(conn: Connection, user_id: int, iid: int) -> dict[str, Any] | None:
    r = (
        conn.execute(
            text(
                """
                SELECT i.id, i.kind, i.display_name, i.aliases,
                       (SELECT count(*) FROM mod_identidades_mentions m
                          WHERE m.resolved_identity_id = i.id) AS mentions,
                       (SELECT string_agg(x.kind || ':' || x.value, ', ')
                          FROM mod_identidades_identifiers x WHERE x.identity_id = i.id) AS idents,
                       (SELECT count(*) FROM mod_identidades c
                          WHERE c.parent_identity_id = i.id) AS children,
                       (SELECT count(*) FROM mod_identidades_person_orgs po
                          WHERE po.person_id = i.id OR po.org_id = i.id)
                       + (SELECT count(*) FROM mod_identidades_link_claims lc
                          WHERE lc.target_id = i.id OR lc.source_id = i.id)
                       + (SELECT count(*) FROM mod_finance_consolidated f
                          WHERE f.counterparty_identity_id = i.id) AS links
                FROM mod_identidades i WHERE i.id = :i AND i.user_id = :u
                """
            ),
            {"i": iid, "u": user_id},
        )
        .mappings()
        .first()
    )
    if r is None:
        return None
    return {
        "id": int(r["id"]),
        "kind": str(r["kind"]),
        "name": str(r["display_name"]),
        "aliases": list(r["aliases"] or [])[:5],
        "mentions": int(r["mentions"]),
        "identifiers": r["idents"] or "",
        # Lo que CUELGA de la ficha: hijos en la jerarquía, afiliaciones, claims, pagos. Una ficha
        # sin menciones pero con esto NO está «sin evidencia»: es un nodo que sostiene vínculos.
        "children": int(r["children"]),
        "links": int(r["links"]),
    }


def _similar(conn: Connection, user_id: int, f: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT id FROM mod_identidades
            WHERE user_id = :u AND id <> :i AND kind = :k
              AND (name_norm LIKE :pfx OR similarity(lower(display_name), lower(:n)) > 0.3)
            ORDER BY similarity(lower(display_name), lower(:n)) DESC LIMIT :l
            """
        ),
        {
            "u": user_id,
            "i": f["id"],
            "k": f["kind"],
            "pfx": f["name"].lower() + "%",
            "n": f["name"],
            "l": _MAX_CANDIDATES,
        },
    ).all()
    out = []
    for (cid,) in rows:
        c = _ficha(conn, user_id, int(cid))
        if c is not None:
            out.append(c)
    return out


def _messages_of(conn: Connection, iid: int) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT source_inbox_ids[1], evidence FROM mod_identidades_mentions "
            "WHERE resolved_identity_id = :i ORDER BY id LIMIT :n"
        ),
        {"i": iid, "n": _MAX_MESSAGES},
    ).all()
    out: list[str] = []
    for inbox_id, evidence in rows:
        if inbox_id is None:
            continue
        payload = conn.execute(
            text("SELECT payload FROM inbox WHERE id = :i"), {"i": int(inbox_id)}
        ).scalar()
        body = render_payload(payload) if payload else ""
        if len(body) > _BODY_CHARS:
            body = body[:_BODY_CHARS] + " […]"
        out.append(f"evidencia: {evidence!r}\n{body}")
    return out


def flagged_identities(
    conn: Connection,
    user_id: int,
    *,
    identity_ids: Sequence[int] | None = None,
    limit: int = 50,
) -> list[Case]:
    """Fichas dudosas aún no verificadas (`metadata.verified_at` vacío), por regla:
    A persona de una palabra · B prefijo de otra ficha del mismo tipo · D org/producto corta sin
    identificadores. `identity_ids` acota (p. ej. las creadas por una ventana)."""
    scope = "" if identity_ids is None else " AND i.id = ANY(CAST(:ids AS BIGINT[]))"
    params: dict[str, Any] = {"u": user_id, "lim": limit, "short": SHORT_ORG_NAME}
    if identity_ids is not None:
        params["ids"] = list(identity_ids)
    rows = conn.execute(
        text(
            f"""
            SELECT i.id,
                   CASE
                     WHEN i.kind = 'persona' AND i.display_name !~ ' ' THEN 'A'
                     WHEN EXISTS (SELECT 1 FROM mod_identidades b
                                  WHERE b.user_id = i.user_id AND b.id <> i.id AND b.kind = i.kind
                                    AND length(i.name_norm) < length(b.name_norm)
                                    AND b.name_norm LIKE i.name_norm || '%') THEN 'B'
                     WHEN i.kind IN ('organizacion','producto')
                          AND length(i.display_name) <= :short
                          AND NOT EXISTS (SELECT 1 FROM mod_identidades_identifiers x
                                          WHERE x.identity_id = i.id) THEN 'D'
                   END AS rule
            FROM mod_identidades i
            WHERE i.user_id = :u AND (i.metadata->>'verified_at') IS NULL{scope}
            ORDER BY i.id
            """
        ),
        params,
    ).all()
    cases: list[Case] = []
    labels = {
        "A": "A persona de una palabra",
        "B": "B prefijo de otra ficha del mismo tipo",
        "D": "D organización corta sin identificadores",
    }
    for iid, rule in rows:
        if rule is None:
            continue
        f = _ficha(conn, user_id, int(iid))
        if f is None:
            continue
        cases.append(
            Case(labels[str(rule)], f, _similar(conn, user_id, f), _messages_of(conn, f["id"]))
        )
        if len(cases) >= limit:
            break
    return cases


def _user_content(case: Case) -> str:
    parts = [f"CASO ({case.rule}): {json.dumps(case.identity, ensure_ascii=False)}"]
    parts.append("CANDIDATAS: " + json.dumps(case.candidates, ensure_ascii=False))
    for i, m in enumerate(case.messages, 1):
        parts.append(f"--- correo {i} ---\n{m}")
    if not case.messages:
        parts.append("(sin correo de origen: la ficha no tiene menciones)")
    return "\n\n".join(parts)


def parse_verdict(content: str, valid_targets: set[int]) -> Verdict:
    """Parseo defensivo: ambigüedad → `separate` (sesgo a no tocar)."""
    keep = Verdict("separate", None, 0.0, "parse_fallback")
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return keep
    if not isinstance(data, dict):
        return keep
    verdict = str(data.get("verdict") or "separate").strip().lower()
    if verdict not in ("attach", "separate", "drop"):
        verdict = "separate"
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
    target = data.get("target_id")
    target_id = (
        int(target) if isinstance(target, int | float) and int(target) in valid_targets else None
    )
    if verdict == "attach" and target_id is None:
        return Verdict("separate", None, conf, "attach_without_valid_target")
    return Verdict(verdict, target_id, conf, str(data.get("rationale") or ""))


async def judge_case(llm: LLMClient, case: Case) -> tuple[Verdict, LLMResult]:
    result = await llm.complete(
        [ChatMessage("system", VERIFY_SYSTEM_PROMPT), ChatMessage("user", _user_content(case))],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return parse_verdict(result.content, {int(c["id"]) for c in case.candidates}), result


def _mark_verified(conn: Connection, iid: int, verdict: Verdict) -> None:
    conn.execute(
        text(
            "UPDATE mod_identidades SET metadata = COALESCE(metadata, '{}'::jsonb) || "
            "CAST(:m AS JSONB), updated_at = NOW() WHERE id = :i"
        ),
        {
            "i": iid,
            "m": json.dumps(
                {
                    "verified_at": datetime.now(UTC).isoformat(),
                    "verified_verdict": verdict.verdict,
                    "verified_rationale": verdict.rationale[:300],
                }
            ),
        },
    )


def _dependents(conn: Connection, iid: int) -> int:
    """Cuántas cosas cuelgan de la ficha (hijos, afiliaciones, claims, pagos): si > 0, borrarla
    rompería vínculos (mismo guard que `DELETE /identidades/{id}`)."""
    return int(
        conn.execute(
            text(
                """
                SELECT (SELECT count(*) FROM mod_identidades WHERE parent_identity_id = :i)
                     + (SELECT count(*) FROM mod_identidades_person_orgs
                        WHERE person_id = :i OR org_id = :i)
                     + (SELECT count(*) FROM mod_identidades_link_claims
                        WHERE target_id = :i OR source_id = :i)
                     + (SELECT count(*) FROM mod_finance_consolidated
                        WHERE counterparty_identity_id = :i)
                """
            ),
            {"i": iid},
        ).scalar_one()
    )


def drop_identity(conn: Connection, user_id: int, iid: int) -> bool:
    """Una ficha que no debió existir: sus menciones vuelven a «sin identidad» (evidencia, no
    ficha), su vértice sale del grafo por `graph_writer`, y la fila se borra (identificadores y
    candidatos caen en cascada). Se NIEGA (False) si algo cuelga de ella —hijos en la jerarquía,
    afiliaciones, claims, pagos—: una ficha sin menciones puede ser un nodo legítimo que el
    curador creó para sostener vínculos; borrarla los rompería."""
    kind = conn.execute(
        text("SELECT kind FROM mod_identidades WHERE id = :i AND user_id = :u"),
        {"i": iid, "u": user_id},
    ).scalar()
    if kind is None:
        return False
    deps = _dependents(conn, iid)
    if deps:
        _log.warning("identidades.verify.drop_refused", identity_id=iid, dependents=deps)
        return False
    conn.execute(
        text(
            "UPDATE mod_identidades_mentions SET resolved_identity_id = NULL, "
            "resolved_kind = NULL, resolution_method = 'unresolved' "
            "WHERE resolved_identity_id = :i"
        ),
        {"i": iid},
    )
    slug = IDENTITY_SLUG_BY_KIND.get(str(kind))
    if slug is not None:
        delete_vertex(conn, user_id, Ref(slug, iid))
    conn.execute(text("DELETE FROM mod_identidades WHERE id = :i"), {"i": iid})
    return True


def apply_verdict(conn: Connection, user_id: int, case: Case, v: Verdict) -> str:
    """Aplica el veredicto (con confianza suficiente) y marca la ficha como verificada.
    Devuelve qué pasó: 'attached' | 'dropped' | 'kept'."""
    iid = case.identity_id
    if (
        v.verdict == "attach"
        and v.target_id is not None
        and v.confidence >= MIN_CONFIDENCE
        and merge_identities(conn, user_id, v.target_id, iid)
    ):
        _mark_verified(conn, v.target_id, v)
        return "attached"
    if v.verdict == "drop" and v.confidence >= MIN_CONFIDENCE and drop_identity(conn, user_id, iid):
        return "dropped"
    _mark_verified(conn, iid, v)
    return "kept"


async def run_verify(
    user_id: int,
    *,
    identity_ids: Sequence[int] | None = None,
    limit: int = 50,
    client: LLMClient | None = None,
    dry_run: bool = False,
) -> VerifyStats:
    """Juzga las fichas dudosas (todas las no verificadas, o `identity_ids`) y aplica. Best-effort
    por caso; `dry_run` solo juzga y loguea. `client` inyectable (tests)."""
    stats = VerifyStats()
    with connection() as conn:
        cases = flagged_identities(conn, user_id, identity_ids=identity_ids, limit=limit)
    if not cases:
        return stats
    owns_client = client is None
    llm: LLMClient = client or build_llm_client(CONSUMER, user_id=user_id)
    _log.info("identidades.verify.start", user_id=user_id, cases=len(cases), dry_run=dry_run)
    try:
        for case in cases:
            stats.cases += 1
            try:
                verdict, result = await judge_case(llm, case)
            except Exception as e:  # best-effort: un caso fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "identidades.verify.case_failed",
                    identity_id=case.identity_id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            outcome = "dry_run"
            if not dry_run:
                with connection() as conn:
                    outcome = apply_verdict(conn, user_id, case, verdict)
                if outcome == "attached":
                    stats.attached += 1
                elif outcome == "dropped":
                    stats.dropped += 1
                else:
                    stats.kept += 1
            record_llm_call(
                user_id=user_id,
                purpose=PURPOSE,
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                source_id=None,
                metadata={
                    "identity_id": case.identity_id,
                    "name": case.identity["name"],
                    "rule": case.rule,
                    "verdict": verdict.verdict,
                    "target_id": verdict.target_id,
                    "confidence": verdict.confidence,
                    "outcome": outcome,
                },
                response_text=result.content,
            )
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
            _log.info(
                "identidades.verify.judged",
                identity_id=case.identity_id,
                name=case.identity["name"],
                rule=case.rule,
                verdict=verdict.verdict,
                target_id=verdict.target_id,
                confidence=verdict.confidence,
                outcome=outcome,
                rationale=verdict.rationale[:200],
            )
    finally:
        if owns_client:
            await aclose_llm(llm)
    _log.info(
        "identidades.verify.done",
        user_id=user_id,
        cases=stats.cases,
        attached=stats.attached,
        dropped=stats.dropped,
        kept=stats.kept,
        errors=stats.errors,
    )
    return stats


# --- gancho por ventana ------------------------------------------------------------------ #


async def run_hygiene_window(user_id: int, inbox_ids: Sequence[int]) -> None:
    """Tras el resolvedor contextual de una ventana: re-resuelve las menciones sueltas (todas, es
    barato) y verifica SOLO las fichas que esa ventana creó."""
    ids = list(inbox_ids)
    if not ids:
        return
    with connection() as conn:
        reresolve_unresolved(conn, user_id)
        created = [
            int(r[0])
            for r in conn.execute(
                text(
                    """
                    SELECT DISTINCT resolved_identity_id FROM mod_identidades_mentions
                    WHERE user_id = :u AND resolution_method = 'created'
                      AND resolved_identity_id IS NOT NULL
                      AND source_inbox_ids && CAST(:ids AS BIGINT[])
                    """
                ),
                {"u": user_id, "ids": ids},
            ).all()
        ]
    if created:
        await run_verify(user_id, identity_ids=created)
