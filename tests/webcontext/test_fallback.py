"""FallbackWebContextProvider: orden, salto ante cualquier error, agotamiento, aclose."""

from __future__ import annotations

from typing import ClassVar

import pytest

from memex.webcontext import (
    EntityKind,
    EntityProfile,
    ProfileResult,
    WebContextError,
    WebContextNotFoundError,
    WebContextProviderError,
    WebContextQuotaError,
)
from memex.webcontext.client import WebContextFormatError
from memex.webcontext.fallback import FallbackWebContextProvider


def _profile(name: str) -> EntityProfile:
    return EntityProfile(name=name, kind="organizacion", one_liner="y")


class _Ok:
    name: ClassVar[str] = "ok"

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls = 0
        self.closed = False

    async def search(self, name: str, kind: EntityKind) -> ProfileResult:
        self.calls += 1
        return ProfileResult(profile=_profile(self.tag), provider=self.tag, latency_ms=1)

    async def aclose(self) -> None:
        self.closed = True


class _Fail:
    name: ClassVar[str] = "fail"

    def __init__(self, exc: WebContextError) -> None:
        self.exc = exc
        self.calls = 0
        self.closed = False

    async def search(self, name: str, kind: EntityKind) -> ProfileResult:
        self.calls += 1
        raise self.exc

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_first_ok_skips_rest() -> None:
    a, b = _Ok("a"), _Ok("b")
    fb = FallbackWebContextProvider([("a", a), ("b", b)])
    result = await fb.search("x", "organizacion")
    assert result.provider == "a"
    assert a.calls == 1
    assert b.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        WebContextProviderError(0, "codex muerto"),
        WebContextQuotaError(429, "cuota"),
        WebContextNotFoundError("x"),
        WebContextFormatError(0, "no valida"),
    ],
)
async def test_falls_through_on_any_error(exc: WebContextError) -> None:
    fail, ok = _Fail(exc), _Ok("b")
    fb = FallbackWebContextProvider([("a", fail), ("b", ok)])
    result = await fb.search("x", "organizacion")
    assert result.provider == "b"
    assert fail.calls == 1
    assert ok.calls == 1


@pytest.mark.asyncio
async def test_all_fail_raises_last() -> None:
    e1 = WebContextProviderError(0, "primero")
    e2 = WebContextQuotaError(429, "ultimo")
    fb = FallbackWebContextProvider([("a", _Fail(e1)), ("b", _Fail(e2))])
    with pytest.raises(WebContextQuotaError) as exc:
        await fb.search("x", "organizacion")
    assert exc.value is e2


@pytest.mark.asyncio
async def test_aclose_closes_all() -> None:
    a, b = _Fail(WebContextProviderError(0, "x")), _Fail(WebContextProviderError(0, "y"))
    fb = FallbackWebContextProvider([("a", a), ("b", b)])
    await fb.aclose()
    assert a.closed
    assert b.closed


def test_empty_chain_raises() -> None:
    with pytest.raises(ValueError):
        FallbackWebContextProvider([])
