"""`procesar_documentos`: reproceso del documento y, seguido, consolidación de finanzas (sin DB ni
LLM: se parchean las dos funciones que delega)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

import memex.modules.finance.consolidate as consolidate_mod
import memex.reprocess as reprocess_mod
from memex.documentos.procesar import STAGES, procesar_documentos


@dataclass
class _Stats:
    groups: int = 3
    consolidated: int = 2
    merges: int = 1
    orphans: int = 0


def test_reprocesa_y_luego_consolida(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    async def fake_reprocess(
        user_id: int, *, stages: list[str], targets: list[int], force: bool = False
    ) -> dict[str, Any]:
        calls.append(("reprocess", (user_id, stages, targets, force)))
        return {"results": {"extract": {"items": 7}}}

    def fake_consolidation(user_id: int) -> _Stats:
        calls.append(("consolidate", user_id))
        return _Stats()

    monkeypatch.setattr(reprocess_mod, "reprocess", fake_reprocess)
    monkeypatch.setattr(consolidate_mod, "run_consolidation", fake_consolidation)

    result = asyncio.run(procesar_documentos(1, [11114]))

    assert result == {"results": {"extract": {"items": 7}}}
    assert calls == [("reprocess", (1, list(STAGES), [11114], True)), ("consolidate", 1)]


def test_consolidacion_fallida_no_tumba_la_subida(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_reprocess(
        user_id: int, *, stages: list[str], targets: list[int], force: bool = False
    ) -> dict[str, Any]:
        return {"results": {"extract": {"items": 2}}}

    def boom(user_id: int) -> _Stats:
        raise RuntimeError("db caída")

    monkeypatch.setattr(reprocess_mod, "reprocess", fake_reprocess)
    monkeypatch.setattr(consolidate_mod, "run_consolidation", boom)

    result = asyncio.run(procesar_documentos(1, [1, 2]))
    assert result["results"]["extract"]["items"] == 2  # lo extraído ya está; la proyección espera
