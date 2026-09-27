"""search_entity: delega en el proveedor (fake-Protocol, sin red)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from memex.webcontext import (
    EntityKind,
    EntityProfile,
    ProfileResult,
    WebContextProvider,
    search_entity,
)


class _StubProvider:
    """Proveedor de prueba que captura (name, kind) y devuelve un perfil fijo."""

    name: ClassVar[str] = "stub"

    def __init__(self) -> None:
        self.received: tuple[str, str] | None = None

    async def search(self, name: str, kind: EntityKind) -> ProfileResult:
        self.received = (name, kind)
        profile = EntityProfile(name=name, kind=kind, one_liner="x")
        return ProfileResult(profile=profile, provider=self.name, latency_ms=1)

    async def aclose(self) -> None:
        return None


def test_stub_satisfies_protocol() -> None:
    assert isinstance(_StubProvider(), WebContextProvider)


@pytest.mark.asyncio
async def test_search_entity_delegates() -> None:
    provider = _StubProvider()
    result = await search_entity(provider, "Rappi", "organizacion")
    assert provider.received == ("Rappi", "organizacion")
    assert result.profile.name == "Rappi"
    assert result.provider == "stub"
