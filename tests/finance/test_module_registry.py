"""Registry + disciplina de Protocol del módulo finance v2."""

from __future__ import annotations

from memex.core.source import SourceKind
from memex.modules import known_modules, resolve
from memex.modules.contract import CAP_EXTRACT, InterestModule
from memex.modules.finance.module import FinanceModule


def test_known_modules_includes_finance() -> None:
    assert "finance" in known_modules()


def test_resolve_finance_builds_module() -> None:
    assert isinstance(resolve("finance")(), FinanceModule)


def test_finance_satisfies_interest_module() -> None:
    assert isinstance(FinanceModule(), InterestModule)


def test_finance_declares_extract_capability() -> None:
    assert CAP_EXTRACT in FinanceModule.capabilities


def test_finance_uses_own_dedup_mechanism() -> None:
    # `()` = mecanismo propio (consolidación), no business-key + upsert_unique.
    assert FinanceModule.identity_fields == ()


def test_finance_consumes_email_and_chat_not_social() -> None:
    assert FinanceModule.consumes_kinds == frozenset({SourceKind.EMAIL, SourceKind.CHAT})


def test_finance_has_no_dependencies() -> None:
    # depends_on=() a propósito: un depends_on duro a identidades apagaría finanzas cuando esté off.
    assert FinanceModule.depends_on == ()
