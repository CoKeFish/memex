"""Tests puros del ruteo (Etapa A): pre-filtro, topo-sort con dependencias, parseo."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from memex.core.source import SourceKind
from memex.modules.contract import CAP_EXTRACT, InterestModule
from memex.modules.finance.schema import TransactionItem
from memex.modules.routing import (
    candidates_for_kind,
    parse_routing,
    resolve_order,
)


def _mod(
    slug: str,
    *,
    depends_on: Iterable[str] = (),
    optional_deps: Iterable[str] = (),
    kinds: frozenset[SourceKind] = frozenset({SourceKind.EMAIL}),
) -> InterestModule:
    cls = type(
        f"Mod_{slug}",
        (),
        {
            "slug": slug,
            "interest": f"interest of {slug}",
            "extraction_schema": TransactionItem,
            "extraction_prompt": "x",
            "capabilities": frozenset({CAP_EXTRACT}),
            "consumes_kinds": kinds,
            "depends_on": tuple(depends_on),
            "optional_deps": tuple(optional_deps),
        },
    )
    return cls()  # type: ignore[no-any-return]


def _active(*mods: InterestModule) -> dict[str, InterestModule]:
    return {m.slug: m for m in mods}


# ----- pre-filtro por consumes_kinds --------------------------------------------- #


def test_candidates_filters_by_kind() -> None:
    finance = _mod("finance", kinds=frozenset({SourceKind.EMAIL, SourceKind.CHAT}))
    social_only = _mod("social", kinds=frozenset({SourceKind.SOCIAL}))
    mods = [finance, social_only]

    assert candidates_for_kind(SourceKind.EMAIL, mods) == [finance]
    assert candidates_for_kind(SourceKind.SOCIAL, mods) == [social_only]
    assert candidates_for_kind(SourceKind.CHAT, mods) == [finance]


# ----- resolve_order (cierre + topo-sort) ---------------------------------------- #


def test_resolve_order_single_no_deps() -> None:
    active = _active(_mod("finance"))
    result = resolve_order(["finance"], active)
    assert result.order == ("finance",)
    assert result.dropped == ()


def test_resolve_order_chain_dep_first() -> None:
    active = _active(_mod("a", depends_on=["b"]), _mod("b"))
    result = resolve_order(["a"], active)  # se agrega b por cierre
    assert result.order == ("b", "a")
    assert result.dropped == ()


def test_resolve_order_diamond() -> None:
    active = _active(
        _mod("a", depends_on=["b", "c"]),
        _mod("b", depends_on=["d"]),
        _mod("c", depends_on=["d"]),
        _mod("d"),
    )
    order = resolve_order(["a"], active).order
    # d antes que b y c; b y c antes que a
    assert order.index("d") < order.index("b")
    assert order.index("d") < order.index("c")
    assert order.index("b") < order.index("a")
    assert order.index("c") < order.index("a")


def test_resolve_order_missing_dep_drops_dependent() -> None:
    active = _active(_mod("a", depends_on=["x"]))  # x no está activo
    result = resolve_order(["a"], active)
    assert result.order == ()
    assert "a" in result.dropped


def test_resolve_order_transitive_drop() -> None:
    active = _active(_mod("a", depends_on=["b"]), _mod("b", depends_on=["x"]))
    result = resolve_order(["a"], active)
    assert result.order == ()
    assert set(result.dropped) == {"a", "b"}


def test_resolve_order_cycle_raises() -> None:
    active = _active(_mod("a", depends_on=["b"]), _mod("b", depends_on=["a"]))
    with pytest.raises(ValueError, match="ciclo"):
        resolve_order(["a"], active)


# ----- optional_deps (dependencia BLANDA: ordena si está, no dropea si no) -------- #


def test_optional_dep_orders_before_when_present() -> None:
    # finance optional-depende de identidades; ambos elegidos → identidades persiste antes.
    active = _active(_mod("identidades"), _mod("finance", optional_deps=["identidades"]))
    result = resolve_order(["finance", "identidades"], active)
    assert result.order.index("identidades") < result.order.index("finance")
    assert result.dropped == ()


def test_optional_dep_absent_does_not_drop() -> None:
    # identidades NO está activo: finance corre igual (no se dropea como con depends_on duro).
    active = _active(_mod("finance", optional_deps=["identidades"]))
    result = resolve_order(["finance"], active)
    assert result.order == ("finance",)
    assert result.dropped == ()


def test_optional_dep_not_force_included() -> None:
    # identidades activo pero NO elegido por el router: no se arrastra al run por la optional dep.
    active = _active(_mod("identidades"), _mod("finance", optional_deps=["identidades"]))
    result = resolve_order(["finance"], active)
    assert result.order == ("finance",)


def test_resolve_order_chosen_not_active_dropped() -> None:
    active = _active(_mod("finance"))
    result = resolve_order(["finance", "ghost"], active)
    assert result.order == ("finance",)
    assert "ghost" in result.dropped


# ----- parse_routing ------------------------------------------------------------- #


def test_parse_routing_happy() -> None:
    assert parse_routing('{"modules": ["finance", "calendar"]}') == ["finance", "calendar"]


def test_parse_routing_empty() -> None:
    assert parse_routing('{"modules": []}') == []


def test_parse_routing_bad_json_returns_none() -> None:
    assert parse_routing("not json") is None


def test_parse_routing_not_list_returns_none() -> None:
    assert parse_routing('{"modules": "finance"}') is None


def test_parse_routing_filters_non_strings() -> None:
    assert parse_routing('{"modules": ["finance", 3, null, "  "]}') == ["finance"]


# ----- módulos concretos: 2 candidatos (finance + calendar) ---------------------- #


def test_finance_and_calendar_are_both_candidates_for_email() -> None:
    """Con finance + calendar (ambos {EMAIL,CHAT}) hay ≥2 candidatos → el orquestador rutea
    por LLM (deja de hacer short-circuit). Acá se valida el pre-filtro + topo-sort puros."""
    from memex.modules.calendar.module import CalendarModule
    from memex.modules.finance.module import FinanceModule

    mods: list[InterestModule] = [FinanceModule(), CalendarModule()]
    for kind in (SourceKind.EMAIL, SourceKind.CHAT):
        assert {m.slug for m in candidates_for_kind(kind, mods)} == {"finance", "calendar"}
    assert candidates_for_kind(SourceKind.SOCIAL, mods) == []

    active = {m.slug: m for m in mods}
    result = resolve_order(["finance", "calendar"], active)
    assert set(result.order) == {"finance", "calendar"}
    assert result.dropped == ()
