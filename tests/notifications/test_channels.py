"""Canales: Telegram (bot falso inyectado), log, registro."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from memex.notifications.channels import ChannelError, build_channel, channel_names
from memex.notifications.channels.log import LogChannel
from memex.notifications.channels.telegram import TelegramChannel
from memex.notifications.client import Notification
from memex.notifications.render import format_message
from memex.notifications.settings import NotificationsEnv


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return SimpleNamespace(message_id=100 + len(self.calls))


def _n() -> Notification:
    return Notification(
        kind="k",
        severity="info",
        title="t",
        body="b",
        dedup_key="d",
        created_at=datetime.now(UTC),
        user_id=1,
    )


@pytest.mark.asyncio
async def test_telegram_sends_chunks_and_returns_last_message_id() -> None:
    bot = FakeBot()
    ch = TelegramChannel("", bot=bot)
    ref = await ch.send("7289", "hola", _n())
    assert ref == "101"
    assert bot.calls == [{"chat_id": 7289, "text": "hola"}]
    long = "x" * 4500
    ref = await ch.send("-100123", long, _n())
    assert ref == "103" and len(bot.calls) == 3
    assert bot.calls[1]["chat_id"] == -100123 and len(bot.calls[1]["text"]) == 4000
    assert len(bot.calls[2]["text"]) == 500


@pytest.mark.asyncio
async def test_telegram_invalid_target_and_missing_token() -> None:
    ch = TelegramChannel("", bot=FakeBot())
    with pytest.raises(ChannelError):
        await ch.send("no-es-numero", "x", _n())
    with pytest.raises(ChannelError):
        TelegramChannel("")


@pytest.mark.asyncio
async def test_telegram_api_error_becomes_channel_error() -> None:
    class Boom:
        async def send_message(self, **kw: Any) -> Any:
            raise RuntimeError("Forbidden: bot was blocked")

    with pytest.raises(ChannelError, match="blocked"):
        await TelegramChannel("", bot=Boom()).send("1", "x", _n())


@pytest.mark.asyncio
async def test_log_channel_and_registry() -> None:
    assert channel_names() == ("log", "telegram")
    env = NotificationsEnv(_env_file=None, telegram_bot_token="123:abc")
    assert isinstance(build_channel("log", env), LogChannel)
    assert isinstance(build_channel("telegram", env), TelegramChannel)
    with pytest.raises(ChannelError):
        build_channel("paloma", env)
    with pytest.raises(ChannelError):
        build_channel("telegram", NotificationsEnv(_env_file=None, telegram_bot_token=""))
    assert await LogChannel().send("", "hola", _n()) is None


def test_format_message_with_and_without_dashboard() -> None:
    assert format_message(title="T", body="B", deep_link="/tareas", dashboard_url="") == "T\nB"
    assert (
        format_message(title="T", body="", deep_link="/tareas", dashboard_url="https://m.local/")
        == "T\nhttps://m.local/tareas"
    )
