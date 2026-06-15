"""Bootstrap del streaming (`memex.api.streaming`) + lifespan de FastAPI.

Integración con DB: inserta sources telegram y verifica qué registra
`build_streaming_runner`. Más un smoke del lifespan vía TestClient context.

Las credenciales de Telegram vienen del VAULT (el resolver ya no acepta env/.env), así que un
source que deba registrarse necesita una cuenta vinculada con sus secretos cifrados.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.api.streaming import build_streaming_runner
from memex.config import settings
from memex.db import connection
from memex.security import vault


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "test-master-key-de-alta-entropia-0123456789")


def _provision_tg_account() -> int:
    """Cuenta telegram con sus credenciales en el VAULT (el resolver ya no acepta env/.env)."""
    with connection() as c:
        vault.provision_user(c, 1, "p")
        aid = c.execute(
            text(
                "INSERT INTO accounts (user_id, alias, provider, kind) "
                "VALUES (1, 'tg-acct', 'telegram', 'chat') RETURNING id"
            ),
        ).scalar()
        assert aid is not None
        aid = int(aid)
        vault.set_secret(c, aid, "api_id", "12345")
        vault.set_secret(c, aid, "api_hash", "deadbeef")
        vault.set_secret(c, aid, "phone", "+34999")
    return aid


def _insert_telegram_source(
    name: str, config: dict[str, Any], *, enabled: bool = True, account_id: int | None = None
) -> int:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type, config, enabled, account_id) "
                "VALUES (1, :name, 'telegram', CAST(:cfg AS JSONB), :enabled, :aid) RETURNING id"
            ),
            {"name": name, "cfg": json.dumps(config), "enabled": enabled, "aid": account_id},
        ).scalar()
    assert sid is not None
    return int(sid)


def test_runner_registers_source_with_streaming_chats() -> None:
    aid = _provision_tg_account()
    _insert_telegram_source(
        "tg-streaming",
        {"allowed_chats": [{"chat_id": -100, "streaming": True}, {"chat_id": -200}]},
        account_id=aid,
    )
    runner = build_streaming_runner()
    assert len(runner._sources) == 1
    assert runner._sources[0].source.type == "telegram"


def test_runner_skips_polling_only_telegram_source() -> None:
    """Un source telegram (con credenciales en el vault) SIN chats streaming no genera listener."""
    aid = _provision_tg_account()
    _insert_telegram_source(
        "tg-polling-only",
        {"allowed_chats": [{"chat_id": -100, "streaming": False}]},
        account_id=aid,
    )
    runner = build_streaming_runner()
    assert len(runner._sources) == 0


def test_runner_empty_when_no_telegram_sources() -> None:
    runner = build_streaming_runner()
    assert len(runner._sources) == 0


def test_runner_skips_disabled_source() -> None:
    aid = _provision_tg_account()
    _insert_telegram_source(
        "tg-disabled",
        {"allowed_chats": [{"chat_id": -100, "streaming": True}]},
        enabled=False,
        account_id=aid,
    )
    runner = build_streaming_runner()
    assert len(runner._sources) == 0


def test_runner_skips_source_without_vault_creds() -> None:
    """Source telegram SIN credenciales en el vault (sin cuenta vinculada) → log + skip, no crash.
    El `.env`/host ya no es fuente: aunque la env var exista, el resolver la quita."""
    _insert_telegram_source(
        "tg-bad",
        {"allowed_chats": [{"chat_id": -100, "streaming": True}]},
    )  # sin account_id → sin credenciales resolubles
    runner = build_streaming_runner()
    assert len(runner._sources) == 0


@pytest.mark.asyncio
async def test_runner_start_stop_no_sources_is_noop() -> None:
    runner = build_streaming_runner()
    await runner.start()
    await runner.stop()  # must not raise with zero sources


def test_lifespan_starts_and_stops_cleanly() -> None:
    """El lifespan de FastAPI arranca/frena el runner sin error (DB vacía de
    telegram → 0 sources). `with TestClient(app)` dispara el lifespan ASGI."""
    from fastapi.testclient import TestClient

    from memex.api.app import app

    with TestClient(app) as client:
        # openapi.json siempre existe en FastAPI → confirma que el app sirvió
        # (lifespan startup completó) y al salir del context frena limpio.
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
