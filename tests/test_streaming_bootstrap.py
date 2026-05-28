"""Bootstrap del streaming (`memex.api.streaming`) + lifespan de FastAPI.

Integración con DB: inserta sources telegram y verifica qué registra
`build_streaming_runner`. Más un smoke del lifespan vía TestClient context.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.api.streaming import build_streaming_runner
from memex.db import connection

_TG_ENV = {
    "MEMEX_TG_API_ID": "12345",
    "MEMEX_TG_API_HASH": "deadbeef",
    "MEMEX_TG_PHONE": "+34999",
}


def _insert_telegram_source(name: str, config: dict[str, Any], *, enabled: bool = True) -> int:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type, config, enabled) "
                "VALUES (1, :name, 'telegram', CAST(:cfg AS JSONB), :enabled) RETURNING id"
            ),
            {"name": name, "cfg": json.dumps(config), "enabled": enabled},
        ).scalar()
    assert sid is not None
    return int(sid)


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _TG_ENV.items():
        monkeypatch.setenv(k, v)


def test_runner_registers_source_with_streaming_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    _insert_telegram_source(
        "tg-streaming",
        {"allowed_chats": [{"chat_id": -100, "streaming": True}, {"chat_id": -200}]},
    )
    runner = build_streaming_runner()
    assert len(runner._sources) == 1
    assert runner._sources[0].source.type == "telegram"


def test_runner_skips_polling_only_telegram_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un source telegram SIN chats streaming no genera listener (solo polling)."""
    _set_env(monkeypatch)
    _insert_telegram_source(
        "tg-polling-only",
        {"allowed_chats": [{"chat_id": -100, "streaming": False}]},
    )
    runner = build_streaming_runner()
    assert len(runner._sources) == 0


def test_runner_empty_when_no_telegram_sources() -> None:
    runner = build_streaming_runner()
    assert len(runner._sources) == 0


def test_runner_skips_disabled_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    _insert_telegram_source(
        "tg-disabled",
        {"allowed_chats": [{"chat_id": -100, "streaming": True}]},
        enabled=False,
    )
    runner = build_streaming_runner()
    assert len(runner._sources) == 0


def test_runner_skips_source_with_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config inválida (env vars faltantes) → log + skip, no crash."""
    # NO seteamos las env vars → from_source_config lanza TelegramConfigError
    monkeypatch.delenv("MEMEX_TG_API_ID", raising=False)
    monkeypatch.delenv("MEMEX_TG_API_HASH", raising=False)
    monkeypatch.delenv("MEMEX_TG_PHONE", raising=False)
    _insert_telegram_source(
        "tg-bad",
        {"allowed_chats": [{"chat_id": -100, "streaming": True}]},
    )
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
