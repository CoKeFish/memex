"""El juez del gate como función reutilizable: UNA llamada LLM sobre un conjunto de correos.

Lo usan dos consumidores con persistencia distinta:
- `gate._judge_batch`: los correos que ninguna regla decidió → veredictos a `relevance_verdicts`.
- `audit`: correos que una regla YA decidió sola → segunda opinión a `relevance_rule_audits`,
  sin tocar el veredicto de la regla.

Por eso acá NO se persiste nada: se arma el prompt (intereses + mensajes renderizados), se llama
al cliente y se parsea; el caller decide qué hacer con el resultado y registra el costo con el
`purpose` que le corresponda.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult
from memex.processing.render import render_payload
from memex.processing.windows import WorkRow
from memex.relevance.interests import list_interests
from memex.relevance.prompts import (
    GATE_SYSTEM_PROMPT,
    build_gate_user_content,
    build_messages_json,
    parse_gate_verdicts,
)
from memex.relevance.settings import GateSettings

#: finish_reason aceptable (convención del repo; el cliente Anthropic ya normaliza end_turn).
OK_FINISH = frozenset({"stop"})
#: Presupuesto de salida: ~64 tokens por veredicto + margen, con piso y techo.
_MAX_TOKENS_FLOOR = 256
_MAX_TOKENS_CEIL = 2048
_TOKENS_PER_VERDICT = 64


def verdict_max_tokens(n: int) -> int:
    return min(_MAX_TOKENS_FLOOR + _TOKENS_PER_VERDICT * n, _MAX_TOKENS_CEIL)


@dataclass(frozen=True)
class JudgeOutcome:
    """Resultado de una llamada al juez: veredictos parseados por inbox_id (None = respuesta
    inutilizable: parse inválido o truncada; `error` dice cuál) + el resultado crudo con usage,
    costo y latencia para que el caller lo registre."""

    verdicts: dict[int, tuple[str, str]] | None
    result: LLMResult
    error: str | None

    @property
    def usable(self) -> bool:
        return self.verdicts is not None


async def ask_judge(
    user_id: int, client: LLMClient, rows: Sequence[WorkRow], settings: GateSettings
) -> JudgeOutcome | None:
    """Una llamada al juez sobre `rows`. None si ningún correo tiene texto que juzgar (el caller
    lo cuenta como `skipped`, sin llamada)."""
    rendered = [render_payload(r.payload, r.ocr_text) for r in rows]
    if not any(part.strip() for part in rendered):
        return None
    with connection() as conn:
        interests = [i["text"] for i in list_interests(conn, user_id, enabled_only=True)]
    messages = [
        ChatMessage("system", GATE_SYSTEM_PROMPT),
        ChatMessage(
            "user", build_gate_user_content(interests, build_messages_json(rows, rendered))
        ),
    ]
    result = await client.complete(
        messages,
        model=settings.complete_model,
        response_format="json_object",
        max_tokens=verdict_max_tokens(len(rows)),
    )
    parsed = (
        parse_gate_verdicts(result.content, {r.inbox_id for r in rows})
        if result.content.strip()
        else None
    )
    truncated = result.finish_reason is not None and result.finish_reason not in OK_FINISH
    if parsed is None or truncated:
        error = "unparseable verdicts" if parsed is None else f"truncated: {result.finish_reason}"
        return JudgeOutcome(None, result, error)
    return JudgeOutcome(parsed, result, None)
