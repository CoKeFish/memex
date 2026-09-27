"""`ImapSource.health_check()`: tipifica la causa cuando el token OAuth ya no sirve.

Un `OAuthError` (Google responde `invalid_grant` cuando el refresh token caducó o el usuario revocó
el acceso) no se arregla reintentando: la única salida es volver a consentir. El resultado debe
decirlo en lenguaje llano y con `code="reauth_required"` para que la tarjeta de /cuenta ofrezca
«Reconectar con Google» en vez de un badge verde con el texto crudo de la excepción en un toast.
"""

from __future__ import annotations

from typing import Any

import pytest

from memex.core.source import HEALTH_CODE_REAUTH_REQUIRED
from memex.ingestors.imap.config import ImapConfig
from memex.ingestors.imap.oauth import OAuthError
from memex.ingestors.imap.source import ImapSource


def _source() -> ImapSource:
    return ImapSource(
        ImapConfig(
            server="imap.gmail.com",
            port=993,
            username="alice@gmail.com",
            auth_method="basic",
            folders=["INBOX"],
            password="x",
        )
    )


class _FailingClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, _cfg: Any) -> _FailingClient:
        return self

    def __enter__(self) -> _FailingClient:
        raise self._exc

    def __exit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio
async def test_oauth_refresh_failure_is_reauth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = OAuthError(
        "token refresh failed: ('invalid_grant: Bad Request', {'error': 'invalid_grant'})"
    )
    monkeypatch.setattr("memex.ingestors.imap.source.ImapClient", _FailingClient(exc))

    res = await _source().health_check()

    assert res.status == "unhealthy"
    assert res.code == HEALTH_CODE_REAUTH_REQUIRED
    assert "Google" in res.detail and "volvé a conectarla" in res.detail
    assert "invalid_grant" in res.detail  # el detalle técnico sigue disponible, al final


@pytest.mark.asyncio
async def test_other_failures_have_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "memex.ingestors.imap.source.ImapClient", _FailingClient(ConnectionError("timeout"))
    )

    res = await _source().health_check()

    assert res.status == "unhealthy"
    assert res.code is None
    assert res.detail.startswith("ConnectionError")
