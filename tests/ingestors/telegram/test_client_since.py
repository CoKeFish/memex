"""Primera corrida de un chat (cursor 0): el cliente pide desde «hoy - since_days», no desde el
mensaje 1. Con cursor avanzado, `min_id` manda y no hay piso de fecha."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from memex.ingestors.telegram.client import TelegramClientWrapper
from memex.ingestors.telegram.config import TelegramConfig

VALID_ENV = {
    "MEMEX_TG_API_ID": "12345",
    "MEMEX_TG_API_HASH": "deadbeefcafebabe",
    "MEMEX_TG_PHONE": "+34999888777",
}


class _FakeTelethon:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def iter_messages(self, entity: Any, **kw: Any) -> Any:
        self.calls.append({"entity": entity, **kw})

        async def _gen() -> Any:
            if False:  # pragma: no cover - generador vacío
                yield None

        return _gen()


def _drain(wrapper: TelegramClientWrapper, *, min_id: int) -> None:
    async def _run() -> None:
        async for _ in wrapper.iter_chat_messages(-100, min_id=min_id, batch_size=50):
            pass

    asyncio.run(_run())


def test_first_run_floors_by_since_days_and_cursor_run_does_not() -> None:
    cfg = TelegramConfig.from_source_config({"since_days": 10}, env=VALID_ENV)
    wrapper = TelegramClientWrapper(cfg)
    fake = _FakeTelethon()
    wrapper._require_client = lambda: fake  # type: ignore[method-assign]
    _drain(wrapper, min_id=0)
    _drain(wrapper, min_id=500)
    first, later = fake.calls
    assert first["min_id"] == 0 and first["reverse"] is True and first["limit"] == 50
    floor = first["offset_date"]
    assert floor is not None
    assert abs((datetime.now(UTC) - timedelta(days=10)) - floor) < timedelta(minutes=1)
    assert later["min_id"] == 500 and later["offset_date"] is None


def test_since_days_zero_means_from_the_beginning() -> None:
    cfg = TelegramConfig.from_source_config({"since_days": 0}, env=VALID_ENV)
    wrapper = TelegramClientWrapper(cfg)
    fake = _FakeTelethon()
    wrapper._require_client = lambda: fake  # type: ignore[method-assign]
    _drain(wrapper, min_id=0)
    assert fake.calls[0]["offset_date"] is None
