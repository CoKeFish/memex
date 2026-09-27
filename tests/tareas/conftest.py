"""Ningún test de tareas habla con un proveedor de verdad.

El juez de tareas (`tareas_judge`) se construye solo si hace falta, así que un test que NO lo
reemplaza termina llamando al proveedor real: paga, tarda y —lo peor— su resultado depende de lo
que opine el modelo ese día. Dos tests de este directorio lo hacían y fallaban de forma
intermitente (2026-09-14: con DeepSeek caído pasaban, con DeepSeek sano el juez fundía una tarea
que el test esperaba encontrar).

Por default el juez dice que NO es la misma tarea: es la respuesta que deja el comportamiento
determinista del módulo intacto. El test que quiera probar la fusión reemplaza `_judge_client`
como ya hacía.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from memex.llm import LLMResult, LLMUsage
from memex.modules.tareas import extract as extract_mod


class _JuezQueNuncaFunde:
    """Juez de mentira: siempre «no es la misma tarea», sin red y sin costo."""

    async def complete(self, messages: Any, **_: Any) -> LLMResult:
        return LLMResult(
            content=json.dumps({"same": False, "confidence": 0.9, "reason": "juez de test"}),
            model="fake-judge",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _juez_de_mentira(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: _JuezQueNuncaFunde())
    yield
