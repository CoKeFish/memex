"""Agrupado puro de la Etapa B (sin DB ni LLM): `_transitive_deps`, `plan_groups`, el parser y el
builder del prompt agrupado."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from memex.core.source import HealthResult, SourceKind
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, InterestModule, ModuleContext
from memex.modules.finance.schema import ExpenseItem
from memex.modules.grouping import (
    _transitive_deps,
    build_grouped_user_content,
    parse_grouped_items,
    plan_groups,
)


class _FakeModule:
    """Módulo mínimo que satisface `InterestModule` para probar el agrupado (solo importa el
    `slug` y `depends_on`; lo demás es relleno válido)."""

    def __init__(self, slug: str, depends_on: tuple[str, ...] = ()) -> None:
        self.slug = slug
        self.interest = f"interés de {slug}"
        self.extraction_schema: type[ExtractionItem] = ExpenseItem
        self.extraction_prompt = f"prompt de {slug}"
        self.capabilities = frozenset({CAP_EXTRACT})
        self.consumes_kinds = frozenset({SourceKind.EMAIL})
        self.depends_on = depends_on

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        return 0

    async def health_check(self) -> HealthResult:
        return HealthResult(status="healthy", detail="", checked_at=datetime.now(UTC))


def _active(*modules: _FakeModule) -> dict[str, InterestModule]:
    # cast: `_FakeModule` cumple el contrato estructuralmente; el agrupado solo lee `.depends_on`.
    return cast("dict[str, InterestModule]", {m.slug: m for m in modules})


# ----- _transitive_deps ----------------------------------------------------------- #


def test_transitive_deps_chain() -> None:
    active = _active(_FakeModule("a"), _FakeModule("b", ("a",)), _FakeModule("c", ("b",)))
    assert _transitive_deps("c", active) == {"a", "b"}
    assert _transitive_deps("a", active) == set()


def test_transitive_deps_skips_inactive() -> None:
    active = _active(_FakeModule("b", ("x",)))  # "x" no está activo
    assert _transitive_deps("b", active) == set()


# ----- plan_groups ---------------------------------------------------------------- #


def test_plan_groups_per_module() -> None:
    active = _active(_FakeModule("a"), _FakeModule("b"), _FakeModule("c"))
    assert plan_groups(["a", "b", "c"], active, "per_module", 3) == [["a"], ["b"], ["c"]]


def test_plan_groups_grouped_size_two() -> None:
    active = _active(_FakeModule("a"), _FakeModule("b"), _FakeModule("c"))
    assert plan_groups(["a", "b", "c"], active, "grouped", 2) == [["a", "b"], ["c"]]


def test_plan_groups_grouped_respects_dep_edge() -> None:
    # b depende de a → no pueden compartir grupo, aunque el cap lo permita
    active = _active(_FakeModule("a"), _FakeModule("b", ("a",)))
    assert plan_groups(["a", "b"], active, "grouped", 2) == [["a"], ["b"]]


def test_plan_groups_all_indep_single_group() -> None:
    active = _active(_FakeModule("a"), _FakeModule("b"), _FakeModule("c"))
    assert plan_groups(["a", "b", "c"], active, "all", 3) == [["a", "b", "c"]]


def test_plan_groups_all_layers_by_deps() -> None:
    active = _active(_FakeModule("a"), _FakeModule("b", ("a",)))
    assert plan_groups(["a", "b"], active, "all", 99) == [["a"], ["b"]]


# ----- parse_grouped_items -------------------------------------------------------- #


def test_parse_grouped_items_valid() -> None:
    content = json.dumps({"finance": [{"amount": "1"}], "calendar": [{"title": "x"}]})
    out = parse_grouped_items(content, ["finance", "calendar"])
    assert out == {"finance": [{"amount": "1"}], "calendar": [{"title": "x"}]}


def test_parse_grouped_items_missing_slug_is_empty() -> None:
    out = parse_grouped_items(json.dumps({"finance": [{"a": 1}]}), ["finance", "calendar"])
    assert out["calendar"] == []


def test_parse_grouped_items_filters_non_dicts() -> None:
    out = parse_grouped_items(json.dumps({"finance": [{"a": 1}, "basura", 3]}), ["finance"])
    assert out == {"finance": [{"a": 1}]}


def test_parse_grouped_items_invalid_json_all_empty() -> None:
    assert parse_grouped_items("no soy json", ["finance", "calendar"]) == {
        "finance": [],
        "calendar": [],
    }


def test_parse_grouped_items_ignores_unknown_keys() -> None:
    out = parse_grouped_items(json.dumps({"finance": [{"a": 1}], "ghost": [{"b": 2}]}), ["finance"])
    assert out == {"finance": [{"a": 1}]}


# ----- build_grouped_user_content ------------------------------------------------- #


def test_build_grouped_user_content_includes_blocks() -> None:
    mods = cast("list[InterestModule]", [_FakeModule("finance"), _FakeModule("calendar")])
    content = build_grouped_user_content(mods, '[{"id": 1, "text": "hola"}]')
    assert "### Módulo: finance" in content
    assert "### Módulo: calendar" in content
    assert "interés de finance" in content
    assert "prompt de calendar" in content
    assert "Esquema del item (JSON Schema):" in content
    assert "Mensajes (JSON):\n[{" in content
