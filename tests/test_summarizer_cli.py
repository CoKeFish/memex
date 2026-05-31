"""Plumbing de los flags de ventana del CLI `memex-summarize` (run_summarization stubeado)."""

from __future__ import annotations

from typing import Any

import pytest

from memex.summarizer.cli import main
from memex.summarizer.worker import SummarizeStats


def test_summarize_cli_threads_window(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    async def fake_run(user: int, **kwargs: object) -> SummarizeStats:
        captured["user"] = user
        captured.update(kwargs)
        return SummarizeStats()

    monkeypatch.setattr("memex.summarizer.cli.run_summarization", fake_run)
    rc = main(["run", "--user", "3", "--max-window-size", "9", "--max-gap-hours", "3"])
    assert rc == 0
    assert captured["user"] == 3
    assert captured["max_window_size"] == 9
    assert captured["max_gap_seconds"] == 10800


def test_summarize_cli_window_defaults(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    async def fake_run(user: int, **kwargs: object) -> SummarizeStats:
        captured.update(kwargs)
        return SummarizeStats()

    monkeypatch.setattr("memex.summarizer.cli.run_summarization", fake_run)
    assert main(["run"]) == 0
    assert captured["max_window_size"] == 40
    assert captured["max_gap_seconds"] == 21600


@pytest.mark.parametrize(
    "argv",
    [["run", "--max-window-size", "0"], ["run", "--max-gap-hours", "0"]],
)
def test_summarize_cli_rejects_bad_args(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        main(argv)
