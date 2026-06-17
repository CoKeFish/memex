"""CLI memex-webcontext: exit codes, wiring del provider, --json, --kind inválido."""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from memex.webcontext import (
    EntityKind,
    EntityProfile,
    ProfileResult,
    WebContextConfigError,
    WebContextNotFoundError,
    WebContextProvider,
)
from memex.webcontext.cli import main


class _StubProvider:
    name: ClassVar[str] = "stub"

    def __init__(
        self, *, result: ProfileResult | None = None, exc: Exception | None = None
    ) -> None:
        self._result = result
        self._exc = exc

    async def search(self, name: str, kind: EntityKind) -> ProfileResult:
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result

    async def aclose(self) -> None:
        return None


def _result() -> ProfileResult:
    profile = EntityProfile(
        name="Rappi", kind="organizacion", one_liner="superapp", sources=("https://rappi.com",)
    )
    return ProfileResult(profile=profile, provider="codex", latency_ms=10)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: WebContextProvider) -> None:
    monkeypatch.setattr("memex.webcontext.cli.build_provider_from_env", lambda **kw: provider)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    # Aísla del .env del repo (load_dotenv camina hacia arriba desde el worktree).
    monkeypatch.setattr("memex.webcontext.cli.load_dotenv", lambda: None)


def test_search_ok(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_provider(monkeypatch, _StubProvider(result=_result()))
    rc = main(["search", "--name", "Rappi", "--kind", "organizacion"])
    assert rc == 0
    assert "Rappi" in capsys.readouterr().out


def test_search_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_provider(monkeypatch, _StubProvider(result=_result()))
    rc = main(["search", "--name", "Rappi", "--kind", "organizacion", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["name"] == "Rappi"
    assert out["provider"] == "codex"
    assert out["sources"] == ["https://rappi.com"]


def test_kind_persona_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["search", "--name", "x", "--kind", "persona"])
    assert exc.value.code == 2


def test_config_error_exit1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(**kw: object) -> WebContextProvider:
        raise WebContextConfigError("falta key")

    monkeypatch.setattr("memex.webcontext.cli.build_provider_from_env", _raise)
    rc = main(["search", "--name", "x", "--kind", "producto"])
    assert rc == 1
    assert "Config" in capsys.readouterr().err


def test_not_found_exit1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(monkeypatch, _StubProvider(exc=WebContextNotFoundError("x")))
    rc = main(["search", "--name", "x", "--kind", "producto"])
    assert rc == 1
    assert "Sin contexto" in capsys.readouterr().err
