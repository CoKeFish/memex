"""Flags `--provider/--model/--codex-model` compartidos (`memex.cli.provider_flags`)."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from memex.cli import provider_flags


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    provider_flags.add_provider_flags(p)
    return p.parse_args(argv)


def test_no_provider_returns_none() -> None:
    assert provider_flags.client_from_flags(_parse([])) is None


def test_provider_builds_single_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    marker = object()

    def fake_build(provider: str, *, model: Any, codex_model: Any) -> Any:
        captured.update(provider=provider, model=model, codex_model=codex_model)
        return marker

    monkeypatch.setattr(provider_flags, "build_provider_client", fake_build)
    out = provider_flags.client_from_flags(_parse(["--provider", "anthropic", "--model", "m"]))
    assert out is marker
    assert captured == {"provider": "anthropic", "model": "m", "codex_model": None}


def test_codex_prints_notice(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(provider_flags, "build_provider_client", lambda *a, **k: "C")
    provider_flags.client_from_flags(_parse(["--provider", "codex", "--codex-model", "gpt-5.1"]))
    assert "codex" in capsys.readouterr().out.lower()


def test_argparse_rejects_unknown_provider() -> None:
    with pytest.raises(SystemExit):
        _parse(["--provider", "mistral"])  # 'openai' ya es válido (provider agregado)
