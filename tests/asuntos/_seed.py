"""Helpers de seed para los tests de asuntos (commit por llamada, visibles para el API)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.llm.client import LLMResult, LLMUsage
from memex.modules.asuntos import store


def source() -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type) VALUES (1, 'imap-test', 'imap') "
                    "RETURNING id"
                )
            ).scalar_one()
        )


def msg(
    src: int,
    *,
    subject: str,
    from_addr: str = "org@example.org",
    from_name: str = "Org",
    body: str = "hola",
    occurred: datetime | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    relevant: bool = True,
) -> int:
    payload: dict[str, Any] = {
        "subject": subject,
        "from": {"address": from_addr, "name": from_name},
        "body_text": body,
    }
    if message_id:
        payload["message_id"] = f"<{message_id}>"
    if in_reply_to:
        payload["in_reply_to"] = f"<{in_reply_to}>"
    if references:
        payload["references"] = references
    with connection() as c:
        iid = int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, :o, CAST(:p AS JSONB)) RETURNING id"
                ),
                {
                    "s": src,
                    "e": f"e-{subject[:20]}-{datetime.now(UTC).timestamp()}",
                    "o": occurred or datetime.now(UTC),
                    "p": json.dumps(payload),
                },
            ).scalar_one()
        )
        if relevant:
            c.execute(
                text(
                    "INSERT INTO relevance_verdicts (user_id, inbox_id, verdict, method) "
                    "VALUES (1, :i, 'relevant', 'rule')"
                ),
                {"i": iid},
            )
    return iid


def asunto(
    name: str, *, kind: str = "programa", status: str = "abierto", seed: str = "otro"
) -> int:
    with connection() as c:
        return store.create_asunto(c, 1, name=name, kind=kind, seed=seed, status=status)


def evidence(aid: int, iid: int, *, method: str = "llm", hito: str | None = None) -> None:
    with connection() as c:
        store.add_evidence(c, 1, aid, iid, method=method, hito_text=hito)


class FakeLLM:
    """Cliente duck-typed: devuelve las respuestas en orden y guarda los prompts que recibió."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(
        self,
        messages: Any,
        *,
        model: str | None = None,
        response_format: str = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.prompts.append(messages[-1].content)
        content = self.responses.pop(0) if self.responses else '{"asignaciones":[]}'
        return LLMResult(
            content=content,
            model="fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )
