"""Ejecutor: solo `memex ...`, sin shell; corre `memex help` de verdad (sin DB)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memex.assistant.executor import (
    CommandExecutor,
    CommandRejected,
    for_humans,
    human_output,
    matches_prefix,
    normalize,
)


def test_normalize_strips_interpreter_prefixes() -> None:
    assert normalize("memex finance register --amount 25000 --currency COP") == [
        "finance",
        "register",
        "--amount",
        "25000",
        "--currency",
        "COP",
    ]
    assert normalize("uv run memex bienestar register --activity gimnasio") == [
        "bienestar",
        "register",
        "--activity",
        "gimnasio",
    ]
    assert normalize("python -m memex.agent_cli asuntos close --id 3") == [
        "asuntos",
        "close",
        "--id",
        "3",
    ]
    assert normalize("/app/.venv/bin/python -m memex.agent_cli finance show --id 1") == [
        "finance",
        "show",
        "--id",
        "1",
    ]
    assert normalize('memex identidad add --name "Ana López" --email a@b.co') == [
        "identidad",
        "add",
        "--name",
        "Ana López",
        "--email",
        "a@b.co",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "ls -la",
        "",
        "memex finance list && rm -rf /",
        "memex finance list | cat",
        "echo memex help",
        'memex finance register --description "sin cerrar',
    ],
)
def test_normalize_rejects(bad: str) -> None:
    with pytest.raises(CommandRejected):
        normalize(bad)


def test_for_humans_drops_json_flag_only() -> None:
    assert (
        for_humans("memex finance register --amount 1 --description 'con --json adentro' --json")
        == "memex finance register --amount 1 --description 'con --json adentro'"
    )
    assert for_humans("memex finance list --q x") == "memex finance list --q x"  # intacto
    assert for_humans("memex 'sin cerrar") == "memex 'sin cerrar"  # no tokeniza: intacto


def test_matches_prefix_on_normalized_tokens() -> None:
    auto = ("finance register", "bienestar register")
    assert matches_prefix("memex finance register --amount 1", auto)
    assert matches_prefix("uv run memex finance register --amount 1", auto)
    assert matches_prefix("memex bienestar register --habit agua", auto)
    assert not matches_prefix("memex finance rm 9", auto)
    assert not matches_prefix("memex calendario add --title x", auto)
    assert not matches_prefix("memex finance register --amount 1", ())
    assert not matches_prefix("ls -la", auto)  # no es memex: no matchea (ni explota)


def test_human_output_drops_structlog_lines() -> None:
    raw = (
        '{"event": "finance.cli.start", "level": "info", "cmd": "list"}\n'
        "\n"
        "2 pago(s):\n"
        "  #1 2026-03-10 -10,000.00 COP\n"
        '{"count": 2, "items": []}\n'
    )
    assert (
        human_output(raw) == '2 pago(s):\n  #1 2026-03-10 -10,000.00 COP\n{"count": 2, "items": []}'
    )
    assert human_output("x" * 50, limit=10).endswith("…")


@pytest.mark.asyncio
async def test_run_executes_memex_help(tmp_path: Path) -> None:
    ex = CommandExecutor([sys.executable, "-m", "memex.agent_cli"], workdir=Path.cwd())
    res = await ex.run("uv run memex help")
    assert res.ok and "memex finance" in res.output
    with pytest.raises(CommandRejected):
        await ex.run("rm -rf /")
