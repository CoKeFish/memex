"""Corrida combinada (resumen + extracción) + CLIs enable/modules (sin red)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.cli import main, main_process
from memex.modules.orchestrator import ExtractStats
from memex.modules.process import CombinedStats, run_combined
from memex.summarizer.worker import SummarizeStats


class FakeCombinedLLM:
    """Devuelve un resumen en modo texto y gastos en modo json_object."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls += 1
        if response_format == "json_object":
            user = messages[-1].content
            arr = json.loads(user[user.index("[") :])
            items = [
                {
                    "source_inbox_ids": [m["id"]],
                    "amount": f"{m['id']}.00",  # distinto por mensaje → 2 vértices (no colapsan)
                    "currency": "ARS",
                    "counterparty": "Test",
                    "occurred_on": None,
                    "description": "gasto",
                    "evidence": m["text"],
                }
                for m in arr
            ]
            content = json.dumps({"items": items})
        else:
            content = "RESUMEN"
        return LLMResult(
            content=content,
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _seed(source_id: int, ext: str, tier: str, payload: dict[str, Any], minute: int = 0) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": ext,
                "occ": datetime(2026, 5, 28, 12, minute, tzinfo=UTC),
                "p": json.dumps(payload),
            },
        ).scalar()
        c.execute(
            text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :iid, :tier)"),
            {"iid": iid, "tier": tier},
        )
    assert iid is not None
    return int(iid)


def _enable(slug: str = "finance") -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_settings (user_id, module_slug, enabled) "
                "VALUES (1, :slug, TRUE) "
                "ON CONFLICT (user_id, module_slug) DO UPDATE SET enabled = TRUE"
            ),
            {"slug": slug},
        )


def _count(table: str) -> int:
    with connection() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)


def test_combined_produces_summary_and_expenses(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _enable()
    _seed(sid, "m1", "batch", {"subject": "luz", "body_text": "pagué $4500"}, minute=0)
    _seed(sid, "m2", "batch", {"subject": "agua", "body_text": "pagué $1200"}, minute=1)

    stats = asyncio.run(run_combined(1, client=FakeCombinedLLM()))

    assert stats.summarize.summaries == 1  # una ventana batch resumida
    assert stats.extract.items == 2  # y dos gastos extraídos
    assert _count("summaries") == 1
    assert _count("mod_finance_transactions") == 2


# ----- CLIs enable / modules (sin LLM) ------------------------------------------- #


def test_cli_modules_lists(capsys: Any) -> None:
    assert main(["modules", "--user", "1"]) == 0
    out = capsys.readouterr().out
    assert "finance: disabled" in out


def test_cli_enable_then_modules(capsys: Any) -> None:
    assert main(["enable", "--module", "finance", "--user", "1"]) == 0
    with connection() as c:
        enabled = c.execute(
            text(
                "SELECT enabled FROM module_settings WHERE user_id = 1 AND module_slug = 'finance'"
            )
        ).scalar()
    assert enabled is True

    assert main(["modules", "--user", "1"]) == 0
    assert "finance: enabled" in capsys.readouterr().out


def test_cli_enable_unknown_module() -> None:
    assert main(["enable", "--module", "ghost", "--user", "1"]) == 1


# ----- plumbing de las perillas por flags (sin DB/LLM, run_* stubeado) ------------ #


def test_extract_cli_threads_tuning(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    async def fake_run(user: int, **kwargs: object) -> ExtractStats:
        captured["user"] = user
        captured.update(kwargs)
        return ExtractStats()

    monkeypatch.setattr("memex.modules.cli.run_extraction", fake_run)
    rc = main(
        [
            "run",
            "--user",
            "2",
            "--max-window-size",
            "5",
            "--max-gap-hours",
            "2",
            "--route-chunk-size",
            "3",
            "--batching-policy",
            "grouped",
            "--group-size",
            "4",
        ]
    )
    assert rc == 0
    assert captured == {
        "user": 2,
        "source_id": None,
        "limit": 200,
        "max_window_size": 5,
        "max_gap_seconds": 7200,
        "route_chunk_size": 3,
        "batching_policy": "grouped",
        "group_size": 4,
    }


def test_extract_cli_defaults(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    async def fake_run(user: int, **kwargs: object) -> ExtractStats:
        captured.update(kwargs)
        return ExtractStats()

    monkeypatch.setattr("memex.modules.cli.run_extraction", fake_run)
    assert main(["run"]) == 0
    assert captured["max_window_size"] == 40
    assert captured["max_gap_seconds"] == 21600  # 6 h por defecto
    assert captured["route_chunk_size"] == 0  # sin split
    assert captured["batching_policy"] == "per_module"
    assert captured["group_size"] == 3


def test_process_cli_threads_tuning(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    async def fake_run(user: int, **kwargs: object) -> CombinedStats:
        captured.update(kwargs)
        return CombinedStats(summarize=SummarizeStats(), extract=ExtractStats())

    monkeypatch.setattr("memex.modules.cli.run_combined", fake_run)
    rc = main_process(
        ["run", "--max-window-size", "7", "--max-gap-hours", "1", "--batching-policy", "all"]
    )
    assert rc == 0
    assert captured["max_window_size"] == 7
    assert captured["max_gap_seconds"] == 3600
    assert captured["batching_policy"] == "all"


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--max-window-size", "0"],
        ["run", "--max-gap-hours", "0"],
        ["run", "--route-chunk-size", "-1"],
        ["run", "--batching-policy", "bogus"],
    ],
)
def test_extract_cli_rejects_bad_args(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        main(argv)
