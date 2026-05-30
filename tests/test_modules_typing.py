"""Disciplina de tipos de los módulos de extracción (ADR-015 / ADR-009).

- El orquestador tipa contra el Protocol `InterestModule`, nunca contra la clase concreta
  `FinanceModule` (igual que el runner tipa contra `MemexSink`, no el cliente HTTP).
- El mapa `_KIND_BY_TYPE` del registry de sources NO diverge del ClassVar `kind` de cada Source.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from memex.ingestors.imap.source import ImapSource
from memex.ingestors.social.source import FacebookSource, InstagramSource, XSource
from memex.ingestors.telegram.source import TelegramSource
from memex.sources import kind_for_type

_ORCHESTRATOR = (
    Path(__file__).resolve().parent.parent / "src" / "memex" / "modules" / "orchestrator.py"
)


def _annotations(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                if arg.annotation:
                    parts.append(ast.unparse(arg.annotation))
            if node.returns:
                parts.append(ast.unparse(node.returns))
        elif isinstance(node, ast.AnnAssign) and node.annotation:
            parts.append(ast.unparse(node.annotation))
    return " ".join(parts)


def test_orchestrator_types_against_interest_module() -> None:
    joined = _annotations(_ORCHESTRATOR)
    assert "InterestModule" in joined, "el orquestador debe anotar contra InterestModule (Protocol)"
    assert "FinanceModule" not in joined, (
        "el orquestador NO debe anotar contra FinanceModule (concreto) — usar el Protocol "
        "InterestModule para que sea agnóstico del módulo"
    )


def test_orchestrator_does_not_import_finance_concrete() -> None:
    tree = ast.parse(_ORCHESTRATOR.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert "memex.modules.finance.module" not in imports, (
        "el orquestador no debe importar FinanceModule; resuelve por el registry (slug)"
    )


@pytest.mark.parametrize(
    "source_cls",
    [ImapSource, TelegramSource, InstagramSource, FacebookSource, XSource],
    ids=lambda c: c.type,
)
def test_kind_map_matches_source_class(source_cls: Any) -> None:
    """`kind_for_type(type)` debe coincidir con el ClassVar `kind` de cada Source."""
    assert kind_for_type(source_cls.type) == source_cls.kind
