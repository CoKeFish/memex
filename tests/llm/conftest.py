"""El corta-circuito es estado de proceso: si un test lo deja abierto, el siguiente ni sale a la
red y falla por una razón que no es la suya. Se limpia antes de cada test."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from memex.llm import breaker


@pytest.fixture(autouse=True)
def _breaker_limpio() -> Iterator[None]:
    breaker.reset()
    yield
    breaker.reset()
