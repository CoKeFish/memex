"""`wait_client_ready`: espera a que el cliente Go exista antes de pedir el código."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from memex.whatsapp.runtime import wait_client_ready


class _Client:
    def __init__(self, ready_after: int, *, fail: bool = False) -> None:
        self.calls = 0
        self.ready_after = ready_after
        self.connect_task: Any = None
        self.fail = fail

    @property
    def is_connected(self) -> Any:
        """Como el cliente asincrono de neonize: la propiedad devuelve una CORRUTINA."""

        async def probe() -> bool:
            self.calls += 1
            if self.calls < self.ready_after:
                raise RuntimeError("client is nil")  # Go antes de registrar el cliente
            return True

        return probe()


@pytest.mark.asyncio
async def test_waits_until_connected() -> None:
    client = _Client(ready_after=3)
    await wait_client_ready(client, timeout_s=5)
    assert client.calls == 3


@pytest.mark.asyncio
async def test_times_out() -> None:
    client = _Client(ready_after=10_000)
    with pytest.raises(TimeoutError):
        await wait_client_ready(client, timeout_s=0.6)


@pytest.mark.asyncio
async def test_propagates_connect_task_failure() -> None:
    client = _Client(ready_after=10_000)

    async def boom() -> None:
        raise ValueError("sin red")

    client.connect_task = asyncio.get_running_loop().create_task(boom())
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="sin red"):
        await wait_client_ready(client, timeout_s=2)
