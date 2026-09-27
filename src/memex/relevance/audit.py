"""Auditoría de las reglas del gate: segunda opinión del juez sobre lo que las reglas deciden solas.

Problema (2026-09-04): una regla es una decisión permanente sobre un remitente, pero los remitentes
cambian lo que mandan. La regla que bloqueaba el boletín de assets de Kronnect (junio) siguió
bloqueando cuando Kronnect pasó a anunciar Player32, un escaparate para juegos indie; nadie se
enteró hasta que el dueño abrió un correo por casualidad. Bloquear no pierde datos (el correo queda
y su veredicto se rehace), lo que se pierde es TIEMPO DE DETECCIÓN.

Mecanismo (la contraparte de la minería: la minería propone reglas a partir de veredictos; esto
propone retirarlas a partir de contradicciones):

1. **Muestreo en línea.** Cada vez que una regla decide un correo suma un «hit». Uno de cada N hits
   (`audit_sample_every`; el 1.º siempre) pasa igual por el juez. Su opinión se guarda en
   `relevance_rule_audits` SIN tocar el veredicto de la regla. Juez y regla en desacuerdo =
   contradicción (block con juez «relevante»; allow con juez «no relevante»); «insufficient» no
   cuenta para ningún lado.
2. **Contadores en la regla** (`hit_count`, `audit_count`, `contradictions`, `last_confirmed_at`,
   `reviewed_at`). `needs_review`: 2+ contradicciones desde la última revisión, o 1 en las 2
   primeras auditorías (señal temprana: «1 de 1» es fuerte, «1 de 10» es ruido).
3. **La contradicción corta el veto en el acto.** Con UNA contradicción la regla deja de
   decidir sola (`rules.decide_sola`): sigue activa, pero sus correos caen al juez en vez de
   cortarse solos. No hay que esperar a que el dueño mire. `needs_review` es otra pregunta —«¿el
   dueño tiene que mirar YA?»— y por eso pide más (2, o 1 muy temprana): molestarlo por una
   contradicción suelta sería ruido; seguir cortando con ella, no.
4. **Revisión con reparación.** Confirmar (la regla vuelve a decidir sola; contador a cero,
   `reviewed_at`) o retirar y RE-JUZGAR lo que esa regla decidió (`rejudge_rule`: apaga la regla
   —`disable=False` la deja activa—, borra sus veredictos no manuales y corre el gate sobre esos
   correos; si otra regla los sigue atrapando, se ve en `by_rule`).
5. **Auditoría retroactiva** (`run_rule_audit`): muestrea hasta K correos ya decididos por cada
   regla activa (los más nuevos, sin repetir) — para estrenar el mecanismo sobre el histórico y
   medir cuántas reglas envejecieron. La primera corrida completa (2026-09-14, 36 reglas, 116
   decisiones) contradijo 10.

Costo: el juez del gate corre en Codex ($0) o en el proveedor configurado; cada auditoría se
registra en `llm_calls` con `purpose='relevance_audit'`, así el gasto es visible aparte del gate.
Superficies: `GET/POST /relevance/rules/{id}/audits|confirm|rejudge`, `POST /relevance/rules/audit`,
`memex-relevance rules audit|audits|confirm|rejudge`, y /filtros (badge «para revisar»).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostBySource, record_llm_call
from memex.db import connection
from memex.llm import LLMClient, LLMQuotaError
from memex.logging import get_logger
from memex.processing.windows import GATE_WINDOW_SIZE, Window, WorkRow
from memex.relevance.judge import ask_judge
from memex.relevance.rules import _ROW_COLS, RuleDecision, set_rule_status
from memex.relevance.settings import GateSettings, get_settings

_log = get_logger("memex.relevance.audit")

#: Contradicciones (desde la última revisión) a partir de las cuales la regla pide revisión.
REVIEW_MIN_CONTRADICTIONS = 2
#: Con pocas auditorías, UNA contradicción ya es señal (1 de 1 o 1 de 2).
REVIEW_EARLY_MAX_AUDITS = 2


@dataclass(frozen=True)
class AuditItem:
    rule_id: int
    effect: str  # 'block' | 'allow'
    inbox_id: int
    judge_verdict: str
    judge_reason: str


@dataclass
class AuditRunStats:
    """Resultado de una corrida de auditoría (en línea o retroactiva)."""

    rules: int = 0
    audited: int = 0
    agreements: int = 0
    contradictions: int = 0
    insufficient: int = 0
    windows: int = 0
    skipped: int = 0
    errors: int = 0
    latency_ms: int = 0
    cost: CostBySource = field(default_factory=CostBySource)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("cost", None)
        d["cost_usd"] = float(self.cost.total.cost_usd)
        return d


# ----------------------------------------------------------------------------- semántica


def agrees(effect: str, judge_verdict: str) -> bool | None:
    """¿El juez coincide con la regla? None = `insufficient` (no cuenta para ningún lado)."""
    if judge_verdict == "insufficient":
        return None
    return (effect == "block") == (judge_verdict == "not_relevant")


def needs_review(row: dict[str, Any]) -> bool:
    """Solo reglas ACTIVAS piden revisión: una apagada ya fue decidida (sus contradicciones quedan
    como historia, no como pendiente)."""
    if str(row.get("status", "active")) != "active":
        return False
    c = int(row.get("contradictions") or 0)
    a = int(row.get("audit_count") or 0)
    return c >= REVIEW_MIN_CONTRADICTIONS or (c >= 1 and a <= REVIEW_EARLY_MAX_AUDITS)


def pick_samples(
    decisions: Sequence[RuleDecision], hit_count_after: dict[int, int], every: int
) -> list[RuleDecision]:
    """Elige qué decisiones auditar: por regla, los hits cuyo número de secuencia (1-based, contando
    desde que existe el contador) cae en 1, 1+N, 1+2N… El primero SIEMPRE se audita: una regla
    recién creada (o recién estrenado el contador) se revisa en su primer correo, no en el 10.º."""
    if every <= 0:
        return []
    per_rule: dict[int, list[RuleDecision]] = {}
    for d in decisions:
        per_rule.setdefault(d.rule_id, []).append(d)
    picked: list[RuleDecision] = []
    for rule_id, ds in per_rule.items():
        after = hit_count_after.get(rule_id, len(ds))
        first_seq = after - len(ds) + 1
        for i, d in enumerate(ds):
            seq = first_seq + i
            if (seq - 1) % every == 0:
                picked.append(d)
    return picked


# ----------------------------------------------------------------------------- persistencia


def bump_hits(conn: Connection, user_id: int, decisions: Sequence[RuleDecision]) -> dict[int, int]:
    """Suma los hits de esta ventana a cada regla; devuelve el `hit_count` resultante por regla."""
    counts: dict[int, int] = {}
    for d in decisions:
        counts[d.rule_id] = counts.get(d.rule_id, 0) + 1
    out: dict[int, int] = {}
    for rule_id, n in counts.items():
        after = conn.execute(
            text(
                "UPDATE relevance_gate_rules SET hit_count = hit_count + :n, last_hit_at = NOW() "
                "WHERE id = :id AND user_id = :uid RETURNING hit_count"
            ),
            {"n": n, "id": rule_id, "uid": user_id},
        ).scalar()
        out[rule_id] = int(after) if after is not None else n
    return out


def record_audits(
    conn: Connection, user_id: int, items: Sequence[AuditItem], *, model: str | None
) -> AuditRunStats:
    """Guarda la segunda opinión por (regla, correo) y actualiza los contadores de la regla.
    Idempotente por par: re-auditar reemplaza la opinión anterior (y no duplica contadores porque
    el UPSERT devuelve si insertó)."""
    stats = AuditRunStats()
    for it in items:
        ok = agrees(it.effect, it.judge_verdict)
        inserted = conn.execute(
            text(
                """
                INSERT INTO relevance_rule_audits
                    (user_id, rule_id, inbox_id, rule_verdict, judge_verdict, judge_reason,
                     model, agrees)
                VALUES (:uid, :rid, :iid, :rv, :jv, :reason, :model, :agrees)
                ON CONFLICT (rule_id, inbox_id) DO UPDATE
                    SET judge_verdict = EXCLUDED.judge_verdict,
                        judge_reason = EXCLUDED.judge_reason, model = EXCLUDED.model,
                        agrees = EXCLUDED.agrees, created_at = NOW()
                RETURNING (xmax = 0) AS inserted
                """
            ),
            {
                "uid": user_id,
                "rid": it.rule_id,
                "iid": it.inbox_id,
                "rv": "not_relevant" if it.effect == "block" else "relevant",
                "jv": it.judge_verdict,
                "reason": it.judge_reason[:1000],
                "model": model,
                "agrees": ok,
            },
        ).scalar()
        if inserted:
            conn.execute(
                text(
                    """
                    UPDATE relevance_gate_rules
                    SET audit_count = audit_count + 1,
                        contradictions = contradictions + CASE WHEN :contra THEN 1 ELSE 0 END,
                        last_confirmed_at = CASE WHEN :agree THEN NOW() ELSE last_confirmed_at END,
                        last_contradiction_at = CASE WHEN :contra THEN NOW()
                                                     ELSE last_contradiction_at END,
                        updated_at = NOW()
                    WHERE id = :rid AND user_id = :uid
                    """
                ),
                {"contra": ok is False, "agree": ok is True, "rid": it.rule_id, "uid": user_id},
            )
        stats.audited += 1
        if ok is None:
            stats.insufficient += 1
        elif ok:
            stats.agreements += 1
        else:
            stats.contradictions += 1
    stats.rules = len({it.rule_id for it in items})
    return stats


def list_rule_audits(
    conn: Connection, user_id: int, rule_id: int, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Las segundas opiniones de una regla, más nuevas primero, con el correo a la vista."""
    rows = (
        conn.execute(
            text(
                """
                SELECT a.id, a.inbox_id, a.rule_verdict, a.judge_verdict, a.judge_reason, a.model,
                       a.agrees, a.created_at, i.occurred_at,
                       i.payload->'from'->>'email' AS sender, i.payload->>'subject' AS subject
                FROM relevance_rule_audits a
                JOIN inbox i ON i.id = a.inbox_id
                WHERE a.user_id = :uid AND a.rule_id = :rid
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT :limit
                """
            ),
            {"uid": user_id, "rid": rule_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def confirm_rule(conn: Connection, user_id: int, rule_id: int) -> dict[str, Any] | None:
    """El dueño revisó la regla y la sostiene: contradicciones a cero desde ahora (la historia queda
    en `relevance_rule_audits`), `reviewed_at`/`last_confirmed_at` = ahora."""
    row = (
        conn.execute(
            text(
                f"""
                UPDATE relevance_gate_rules
                SET reviewed_at = NOW(), last_confirmed_at = NOW(), contradictions = 0,
                    updated_at = NOW()
                WHERE id = :id AND user_id = :uid AND status <> 'rejected'
                RETURNING {_ROW_COLS}
                """
            ),
            {"id": rule_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def rule_decided_inbox_ids(conn: Connection, user_id: int, rule_id: int) -> list[int]:
    rows = conn.execute(
        text(
            "SELECT inbox_id FROM relevance_verdicts "
            "WHERE user_id = :uid AND rule_id = :rid AND method = 'rule' ORDER BY inbox_id"
        ),
        {"uid": user_id, "rid": rule_id},
    ).all()
    return [int(r[0]) for r in rows]


# ----------------------------------------------------------------------------- en línea (gate)


def _record_audit_cost(
    user_id: int,
    window_source_id: int,
    outcome_result: Any,
    stats: AuditRunStats,
    *,
    status: str,
    n: int,
    mode: str,
    error: str | None = None,
) -> None:
    record_llm_call(
        user_id=user_id,
        purpose="relevance_audit",
        model=outcome_result.model,
        prompt_tokens=outcome_result.usage.prompt_tokens,
        completion_tokens=outcome_result.usage.completion_tokens,
        cache_hit_tokens=outcome_result.usage.cache_hit_tokens,
        cost_usd=outcome_result.cost_usd,
        latency_ms=outcome_result.latency_ms,
        status=status,
        source_id=window_source_id,
        error_message=error,
        metadata={"n": n, "mode": mode, "audit": True},
        response_text=outcome_result.content or None,
    )
    stats.cost.record(
        window_source_id,
        prompt_tokens=outcome_result.usage.prompt_tokens,
        completion_tokens=outcome_result.usage.completion_tokens,
        cost_usd=outcome_result.cost_usd,
    )
    stats.latency_ms += int(outcome_result.latency_ms or 0)


async def audit_rule_decisions(
    user_id: int,
    client: LLMClient,
    window: Window,
    decisions: Sequence[RuleDecision],
    settings: GateSettings,
) -> AuditRunStats:
    """Muestreo en línea: suma hits, elige 1 de N por regla y pide la segunda opinión en UNA
    llamada para toda la ventana. Best-effort: un fallo del juez se loguea y no toca el gate."""
    stats = AuditRunStats()
    with connection() as conn:
        hits = bump_hits(conn, user_id, decisions)
    sampled = pick_samples(decisions, hits, settings.audit_sample_every)
    if not sampled:
        return stats
    by_id = {r.inbox_id: r for r in window.rows}
    rows = [by_id[d.inbox_id] for d in sampled if d.inbox_id in by_id]
    if not rows:
        return stats
    outcome = await ask_judge(user_id, client, rows, settings)
    stats.windows += 1
    if outcome is None:
        stats.skipped += len(rows)
        return stats
    if not outcome.usable:
        stats.errors += 1
        _record_audit_cost(
            user_id,
            window.source_id,
            outcome.result,
            stats,
            status="error",
            n=len(rows),
            mode=settings.mode,
            error=outcome.error,
        )
        _log.warning(
            "relevance.audit.window_unusable",
            source_id=window.source_id,
            inbox_ids=[r.inbox_id for r in rows],
            error=outcome.error,
        )
        return stats
    assert outcome.verdicts is not None
    items = [
        AuditItem(d.rule_id, d.effect, d.inbox_id, *outcome.verdicts[d.inbox_id])
        for d in sampled
        if d.inbox_id in outcome.verdicts
    ]
    with connection() as conn:
        rec = record_audits(conn, user_id, items, model=outcome.result.model)
    stats.audited, stats.agreements = rec.audited, rec.agreements
    stats.contradictions, stats.insufficient, stats.rules = (
        rec.contradictions,
        rec.insufficient,
        rec.rules,
    )
    _record_audit_cost(
        user_id,
        window.source_id,
        outcome.result,
        stats,
        status="ok",
        n=len(rows),
        mode=settings.mode,
    )
    _log.info(
        "relevance.audit.window",
        source_id=window.source_id,
        audited=stats.audited,
        contradictions=stats.contradictions,
        rules=sorted({d.rule_id for d in sampled}),
        contradicted_rules=sorted(
            {it.rule_id for it in items if agrees(it.effect, it.judge_verdict) is False}
        ),
    )
    return stats


# ----------------------------------------------------------------------------- retroactiva


def _load_rows(user_id: int, inbox_ids: Sequence[int]) -> list[WorkRow]:
    """Filas para el juez (mismo shape que el work-set del gate) de correos YA decididos."""
    if not inbox_ids:
        return []
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT i.id, i.source_id, i.occurred_at, i.payload,
                           COALESCE(c.tier, 'batch') AS tier, s.type AS source_type,
                           COALESCE(ma.ocr_text, '') AS ocr_text
                    FROM inbox i
                    JOIN sources s ON s.id = i.source_id
                    LEFT JOIN classifications c ON c.inbox_id = i.id
                    LEFT JOIN (
                        SELECT inbox_id, string_agg(ocr_text, E'\\n' ORDER BY id) AS ocr_text
                        FROM media_assets
                        WHERE ocr_status = 'ok' AND ocr_text IS NOT NULL AND ocr_text <> ''
                        GROUP BY inbox_id
                    ) ma ON ma.inbox_id = i.id
                    WHERE i.user_id = :uid AND i.id = ANY(:iids)
                    ORDER BY i.id
                    """
                ),
                {"uid": user_id, "iids": list(inbox_ids)},
            )
            .mappings()
            .all()
        )
    return [
        WorkRow(
            inbox_id=int(r["id"]),
            source_id=int(r["source_id"]),
            occurred_at=r["occurred_at"],
            payload=dict(r["payload"] or {}),
            tier=str(r["tier"]),
            source_type=str(r["source_type"] or ""),
            ocr_text=str(r["ocr_text"] or ""),
        )
        for r in rows
    ]


def audit_candidates(
    conn: Connection, user_id: int, *, per_rule: int, rule_ids: Sequence[int] | None = None
) -> list[tuple[int, str, int]]:
    """(rule_id, effect, inbox_id): hasta `per_rule` correos decididos por cada regla activa, los
    más nuevos primero, saltando los ya auditados."""
    rid_filter = " AND r.id = ANY(:rids)" if rule_ids else ""
    rows = conn.execute(
        text(
            f"""
            SELECT rule_id, effect, inbox_id FROM (
                SELECT v.rule_id, r.effect, v.inbox_id,
                       ROW_NUMBER() OVER (PARTITION BY v.rule_id ORDER BY v.created_at DESC,
                                          v.inbox_id DESC) AS rn
                FROM relevance_verdicts v
                JOIN relevance_gate_rules r ON r.id = v.rule_id
                WHERE v.user_id = :uid AND v.method = 'rule' AND r.status = 'active'{rid_filter}
                  AND NOT EXISTS (SELECT 1 FROM relevance_rule_audits a
                                   WHERE a.rule_id = v.rule_id AND a.inbox_id = v.inbox_id)
            ) t WHERE rn <= :per_rule
            ORDER BY rule_id, rn
            """
        ),
        {"uid": user_id, "per_rule": per_rule, "rids": list(rule_ids or [])},
    ).all()
    return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]


async def run_rule_audit(
    user_id: int,
    *,
    per_rule: int = 3,
    rule_ids: Sequence[int] | None = None,
    client: LLMClient | None = None,
    max_window_size: int = GATE_WINDOW_SIZE,
) -> AuditRunStats:
    """Auditoría retroactiva: segunda opinión sobre hasta `per_rule` correos por regla activa.
    Ventanas de `max_window_size` (mezclando reglas; al juez le da igual). Best-effort por
    ventana; `LLMQuotaError` aborta."""
    from memex.relevance.providers import build_gate_client

    stats = AuditRunStats()
    with connection() as conn:
        settings = get_settings(conn, user_id)
        if not settings.enabled:
            _log.info("relevance.audit.disabled", user_id=user_id)
            return stats
        cands = audit_candidates(conn, user_id, per_rule=per_rule, rule_ids=rule_ids)
    if not cands:
        _log.info("relevance.audit.run.empty", user_id=user_id)
        return stats
    by_inbox: dict[int, tuple[int, str]] = {iid: (rid, eff) for rid, eff, iid in cands}
    rows = _load_rows(user_id, list(by_inbox))
    owns = client is None
    active = client if client is not None else build_gate_client(settings)
    try:
        for start in range(0, len(rows), max_window_size):
            chunk = rows[start : start + max_window_size]
            stats.windows += 1
            try:
                outcome = await ask_judge(user_id, active, chunk, settings)
            except LLMQuotaError:
                _log.error("relevance.audit.aborted_no_quota", user_id=user_id)
                raise
            except Exception as e:  # una ventana fallida no frena las demás
                stats.errors += 1
                _log.error(
                    "relevance.audit.window_failed",
                    inbox_ids=[r.inbox_id for r in chunk],
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            if outcome is None:
                stats.skipped += len(chunk)
                continue
            src = chunk[0].source_id
            if not outcome.usable:
                stats.errors += 1
                _record_audit_cost(
                    user_id,
                    src,
                    outcome.result,
                    stats,
                    status="error",
                    n=len(chunk),
                    mode=settings.mode,
                    error=outcome.error,
                )
                continue
            assert outcome.verdicts is not None
            items = [
                AuditItem(by_inbox[iid][0], by_inbox[iid][1], iid, verdict, reason)
                for iid, (verdict, reason) in outcome.verdicts.items()
                if iid in by_inbox
            ]
            with connection() as conn:
                rec = record_audits(conn, user_id, items, model=outcome.result.model)
            stats.audited += rec.audited
            stats.agreements += rec.agreements
            stats.contradictions += rec.contradictions
            stats.insufficient += rec.insufficient
            _record_audit_cost(
                user_id, src, outcome.result, stats, status="ok", n=len(chunk), mode=settings.mode
            )
    finally:
        if owns and hasattr(active, "aclose"):
            await active.aclose()
    stats.rules = len({rid for rid, _e, _i in cands})
    _log.info("relevance.audit.run.end", user_id=user_id, **stats.as_dict())
    return stats


# ----------------------------------------------------------------------------- retiro + reparación


async def rejudge_rule(
    user_id: int, rule_id: int, *, disable: bool = True, client: LLMClient | None = None
) -> dict[str, Any] | None:
    """Retira la regla (opcional) y re-juzga lo que decidió: borra sus veredictos NO manuales y
    corre el gate sobre esos correos. Si otra regla los sigue atrapando, se ve en `by_rule`.
    None si la regla no existe."""
    from memex.relevance.gate import run_relevance_gate

    with connection() as conn:
        row = (
            conn.execute(
                text(
                    f"SELECT {_ROW_COLS} FROM relevance_gate_rules "
                    "WHERE id = :id AND user_id = :uid"
                ),
                {"id": rule_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        rule = dict(row)
        if disable and rule["status"] == "active":
            updated = set_rule_status(conn, rule_id, user_id, "disabled")
            if updated is not None:
                rule = updated
        targets = rule_decided_inbox_ids(conn, user_id, rule_id)
    gate: dict[str, Any] = {}
    if targets:
        stats = await run_relevance_gate(user_id, inbox_ids=targets, force=True, client=client)
        gate = {
            "windows": stats.windows,
            "messages": stats.messages,
            "relevant": stats.relevant,
            "not_relevant": stats.not_relevant,
            "insufficient": stats.insufficient,
            "by_rule": stats.by_rule,
            "errors": stats.errors,
            "cost_usd": float(stats.cost.total.cost_usd),
        }
    _log.info(
        "relevance.rule.rejudged",
        user_id=user_id,
        rule_id=rule_id,
        disabled=disable,
        targets=len(targets),
        **gate,
    )
    return {"rule": rule, "targets": len(targets), "gate": gate}


__all__ = [
    "AuditItem",
    "AuditRunStats",
    "agrees",
    "audit_candidates",
    "audit_rule_decisions",
    "bump_hits",
    "confirm_rule",
    "list_rule_audits",
    "needs_review",
    "pick_samples",
    "record_audits",
    "rejudge_rule",
    "rule_decided_inbox_ids",
    "run_rule_audit",
]
