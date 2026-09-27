"""Worker del gate de relevancia: veredicto por correo ANTES de resumen/extracción.

Espejo estructural de `relations.summary.run_summaries` (mismo contrato best-effort por
ventana, `LLMQuotaError` aborta, cliente inyectable, dead-letter al fallar el parse), pero el
LLM default es ANTHROPIC (Opus — decisión del dueño: modelo superior para el portero), no el
DeepSeek compartido del resto del pipeline.

Flujo por ventana:
1. Reglas deterministas activas primero (`apply_active_rules`): lo matcheado se persiste con
   `method='rule'` SIN gastar LLM — `block`→`not_relevant`, `allow`→`relevant`. Si un correo
   matchea reglas de AMBAS polaridades es un conflicto: no se cortocircuita, cae al juez. La regla
   que el auditor contradijo tampoco decide sola (pierde el veto hasta que el dueño la revise):
   sus correos caen al juez igual que un conflicto.
2. El resto va a Opus según el modo del experimento (`per_window`: 1 llamada con veredictos
   por mensaje; `per_message`: 1 llamada por correo).
3. Veredictos a `relevance_verdicts` (idempotente); el costo a `llm_calls` con
   `purpose="relevance_gate"`.

Sin `trace_nodes`: `create_root` es delete-then-write por inbox y pisaría la traza de
extracción; la traza de la decisión ES el veredicto persistido + la llm_call correlacionada
(`bound_log_context` con source_id, e inbox_id si la ventana es de 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from memex.core.deadletter import STAGE_RELEVANCE, record_failures
from memex.core.observability import CostBySource, record_llm_call
from memex.db import connection
from memex.llm import AnthropicClient, LLMClient, LLMQuotaError
from memex.logging import bound_log_context, get_logger
from memex.processing.windows import (
    GATE_WINDOW_SIZE,
    Window,
    WorkRow,
    plan_windows,
)
from memex.relevance.audit import audit_rule_decisions
from memex.relevance.judge import ask_judge
from memex.relevance.providers import build_gate_client
from memex.relevance.rules import apply_active_rules
from memex.relevance.settings import GateSettings, get_settings
from memex.relevance.verdicts import VerdictItem, clear_verdicts, insert_verdicts, load_gate_workset

_log = get_logger("memex.relevance.gate")

_DEFAULT_LIMIT = 200


@dataclass
class GateStats:
    """Resultado de una corrida del gate."""

    windows: int = 0
    messages: int = 0
    relevant: int = 0
    not_relevant: int = 0
    insufficient: int = 0
    by_rule: int = 0
    skipped: int = 0
    errors: int = 0
    #: Auditoría de reglas (segunda opinión del juez sobre lo decidido por regla; ver audit.py).
    audited: int = 0
    audit_contradictions: int = 0
    cost: CostBySource = field(default_factory=CostBySource)

    def bump(self, verdict: str) -> None:
        if verdict == "relevant":
            self.relevant += 1
        elif verdict == "not_relevant":
            self.not_relevant += 1
        else:
            self.insufficient += 1


def _record_cost(
    user_id: int,
    window: Window,
    stats: GateStats,
    *,
    status: str,
    model: str,
    mode: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: Decimal,
    latency_ms: int,
    cache_hit_tokens: int = 0,
    error_message: str | None = None,
    response_text: str | None = None,
    verdict_counts: dict[str, int] | None = None,
) -> None:
    record_llm_call(
        user_id=user_id,
        purpose="relevance_gate",
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        status=status,
        source_id=window.source_id,
        error_message=error_message,
        metadata={"n": len(window.rows), "mode": mode, **(verdict_counts or {})},
        response_text=response_text,
    )
    stats.cost.record(
        window.source_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
    )


async def _judge_batch(
    user_id: int,
    client: LLMClient,
    window: Window,
    rows: list[WorkRow],
    settings: GateSettings,
    stats: GateStats,
) -> None:
    """UNA llamada LLM para `rows` (la ventana entera en per_window; un correo en per_message).

    Persiste los veredictos solo si el parse cubre el lote; parse inválido → llm_call en
    error + dead-letter (reintentable), sin veredictos. La llamada en sí es `ask_judge`
    (compartida con la auditoría de reglas).
    """
    outcome = await ask_judge(user_id, client, rows, settings)
    if outcome is None:
        stats.skipped += len(rows)
        _log.warning(
            "relevance.gate.empty_input",
            source_id=window.source_id,
            inbox_ids=[r.inbox_id for r in rows],
        )
        return
    result = outcome.result
    if not outcome.usable:
        error = outcome.error or "unusable"
        stats.errors += 1
        _log.warning(
            "relevance.gate.window_unusable",
            source_id=window.source_id,
            inbox_ids=[r.inbox_id for r in rows],
            finish_reason=result.finish_reason,
            error=error,
        )
        _record_cost(
            user_id,
            window,
            stats,
            status="error",
            model=result.model,
            mode=settings.mode,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            error_message=error,
            response_text=result.content or None,
        )
        record_failures(user_id, STAGE_RELEVANCE, [r.inbox_id for r in rows], error)
        return

    assert outcome.verdicts is not None
    items = [
        VerdictItem(
            inbox_id=iid,
            verdict=verdict,
            method="llm",
            reason=reason,
            model=result.model,
            mode=settings.mode,
        )
        for iid, (verdict, reason) in outcome.verdicts.items()
    ]
    with connection() as conn:
        insert_verdicts(conn, user_id, items)
    counts: dict[str, int] = {}
    for it in items:
        counts[it.verdict] = counts.get(it.verdict, 0) + 1
        stats.bump(it.verdict)
    stats.messages += len(items)
    _record_cost(
        user_id,
        window,
        stats,
        status="ok",
        model=result.model,
        mode=settings.mode,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        response_text=result.content,
        verdict_counts=counts,
    )


async def _process_window(
    user_id: int,
    client: LLMClient,
    window: Window,
    settings: GateSettings,
    stats: GateStats,
) -> None:
    """Procesa UNA ventana: reglas deterministas primero, el resto al LLM según el modo."""
    with connection() as conn:
        application = apply_active_rules(conn, user_id, list(window.rows))
        if application.decisions:
            insert_verdicts(
                conn,
                user_id,
                [
                    VerdictItem(
                        inbox_id=d.inbox_id,
                        verdict="relevant" if d.effect == "allow" else "not_relevant",
                        method="rule",
                        rule_id=d.rule_id,
                        reason=f"regla {d.effect} del gate",
                    )
                    for d in application.decisions
                ],
            )
    decisions = application.decisions
    if decisions:
        n_allow = sum(1 for d in decisions if d.effect == "allow")
        n_block = len(decisions) - n_allow
        stats.by_rule += len(decisions)
        stats.relevant += n_allow
        stats.not_relevant += n_block
        stats.messages += len(decisions)
        _log.info(
            "relevance.gate.by_rule",
            source_id=window.source_id,
            inbox_ids=sorted(d.inbox_id for d in decisions),
            block=n_block,
            allow=n_allow,
            rules=sorted({d.rule_id for d in decisions}),
        )
    # Auditoría por muestreo: 1 de cada N decisiones por regla pasa igual por el juez (segunda
    # opinión, sin tocar el veredicto). Best-effort: nunca frena la ventana. Ver audit.py.
    if decisions and settings.audit_sample_every > 0:
        try:
            audit = await audit_rule_decisions(user_id, client, window, decisions, settings)
        except LLMQuotaError:
            raise
        except Exception as e:
            _log.warning(
                "relevance.audit.failed",
                source_id=window.source_id,
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )
        else:
            stats.audited += audit.audited
            stats.audit_contradictions += audit.contradictions
            stats.cost.record(
                window.source_id,
                prompt_tokens=audit.cost.total.prompt_tokens,
                completion_tokens=audit.cost.total.completion_tokens,
                cost_usd=audit.cost.total.cost_usd,
            )
    # Una regla que matchea está bajo sospecha (el auditor la contradijo): el correo lo decide el
    # juez, no la regla. Se loguea para poder explicar por qué ese correo costó una llamada.
    if application.under_audit:
        _log.info(
            "relevance.gate.rule_under_audit",
            source_id=window.source_id,
            inbox_ids=sorted(i for i, _ in application.under_audit),
            rules=sorted({r for _, r in application.under_audit}),
        )
    # Conflicto allow↔block en el mismo correo: NO se cortocircuita, cae al juez (y se loguea).
    for c in application.conflicts:
        _log.info(
            "relevance.gate.rule_conflict",
            source_id=window.source_id,
            inbox_id=c.inbox_id,
            block_rule_id=c.block_rule_id,
            allow_rule_id=c.allow_rule_id,
        )

    decided = {d.inbox_id for d in decisions}
    pending = [r for r in window.rows if r.inbox_id not in decided]
    if not pending:
        return
    if settings.mode == "per_message":
        for row in pending:
            await _judge_batch(user_id, client, window, [row], settings, stats)
    else:
        await _judge_batch(user_id, client, window, pending, settings, stats)


async def run_relevance_gate(
    user_id: int,
    *,
    source_id: int | None = None,
    limit: int = _DEFAULT_LIMIT,
    max_window_size: int = GATE_WINDOW_SIZE,
    inbox_ids: list[int] | None = None,
    force: bool = False,
    client: LLMClient | None = None,
) -> GateStats:
    """Corre el gate sobre los correos pendientes-de-veredicto. Apagado → no-op.

    `inbox_ids` acota a un set explícito (etapa `relevance` del reproceso); `force` borra
    primero los veredictos NO manuales de esos targets (el juicio del dueño no se pisa).
    `client` inyectable (tests con fake / override del CLI); por default lo decide
    `settings.provider` (Anthropic u, host-side, codex). Best-effort por ventana;
    `LLMQuotaError` aborta la corrida (saldo agotado).
    """
    with connection() as conn:
        settings = get_settings(conn, user_id)
        if not settings.enabled:
            _log.info("relevance.gate.disabled", user_id=user_id)
            return GateStats()
        if force and inbox_ids:
            cleared = clear_verdicts(conn, user_id, inbox_ids, keep_manual=True)
            if cleared:
                _log.info("relevance.gate.force_cleared", n=cleared, inbox_ids=inbox_ids)

    stats = GateStats()
    eff_limit = max(limit, len(inbox_ids)) if inbox_ids is not None else limit
    windows = plan_windows(
        load_gate_workset(user_id, source_id=source_id, limit=eff_limit, inbox_ids=inbox_ids),
        max_window_size=max_window_size,
    )
    if not windows:
        _log.info("relevance.gate.run.empty", user_id=user_id, source_id=source_id)
        return stats

    owns_client = client is None
    active: LLMClient = client if client is not None else build_gate_client(settings)
    try:
        for window in windows:
            stats.windows += 1
            with bound_log_context(
                source_id=window.source_id,
                inbox_id=window.rows[0].inbox_id if len(window.rows) == 1 else None,
            ):
                try:
                    await _process_window(user_id, active, window, settings, stats)
                except LLMQuotaError:
                    _log.error("relevance.gate.aborted_no_quota", source_id=window.source_id)
                    raise
                except Exception as e:  # best-effort: una ventana fallida no frena las demás
                    stats.errors += 1
                    _log.error(
                        "relevance.gate.window_failed",
                        source_id=window.source_id,
                        n=len(window.rows),
                        inbox_ids=[r.inbox_id for r in window.rows],
                        exc_type=type(e).__name__,
                        exc_msg=str(e),
                    )
                    _record_cost(
                        user_id,
                        window,
                        stats,
                        status="error",
                        model="unknown",
                        mode=settings.mode,
                        prompt_tokens=0,
                        completion_tokens=0,
                        cost_usd=Decimal("0"),
                        latency_ms=0,
                        error_message=str(e)[:500],
                    )
                    record_failures(
                        user_id, STAGE_RELEVANCE, [r.inbox_id for r in window.rows], str(e)
                    )
    finally:
        if owns_client and isinstance(active, AnthropicClient):
            await active.aclose()

    _log.info(
        "relevance.gate.run.end",
        user_id=user_id,
        windows=stats.windows,
        messages=stats.messages,
        relevant=stats.relevant,
        not_relevant=stats.not_relevant,
        insufficient=stats.insufficient,
        by_rule=stats.by_rule,
        skipped=stats.skipped,
        errors=stats.errors,
        audited=stats.audited,
        audit_contradictions=stats.audit_contradictions,
        mode=settings.mode,
        **stats.cost.log_fields(),
    )
    return stats
