"""Cableado del seam `ctx.deps`: el orquestador inyecta, por cada dependencia declarada en
`depends_on` que expone `provide_domain`, su handle tipado. Cubre `_build_deps` (selección pura) +
el handle REAL de identidades resolviendo una identidad sembrada (end-to-end del seam)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.contract import CAP_EXTRACT, CAP_PROVIDE_DOMAIN
from memex.modules.identidades.domain import IdentidadesDomainReader
from memex.modules.identidades.module import IdentidadesModule
from memex.modules.orchestrator import _build_deps


class _FakeProvider:
    slug = "prov"
    capabilities = frozenset({CAP_PROVIDE_DOMAIN})
    depends_on: tuple[str, ...] = ()
    optional_deps: tuple[str, ...] = ()

    def provide_domain(self, conn: Any, user_id: int) -> tuple[str, int]:
        return ("handle", user_id)


class _FakeConsumer:
    slug = "cons"
    capabilities = frozenset({CAP_EXTRACT})

    def __init__(
        self, depends_on: tuple[str, ...] = (), optional_deps: tuple[str, ...] = ()
    ) -> None:
        self.depends_on = depends_on
        self.optional_deps = optional_deps


class _CapButNoMethod:
    """Declara la capacidad pero NO implementa `provide_domain` → no se inyecta."""

    slug = "fake"
    capabilities = frozenset({CAP_PROVIDE_DOMAIN})
    depends_on: tuple[str, ...] = ()
    optional_deps: tuple[str, ...] = ()


def test_build_deps_injects_declared_provider() -> None:
    prov: Any = _FakeProvider()
    cons: Any = _FakeConsumer(depends_on=("prov",))
    conn: Any = None  # el fake provider no usa la conexión
    deps = _build_deps(cons, conn, 7, {"prov": prov, "cons": cons})
    assert deps == {"prov": ("handle", 7)}


def test_build_deps_empty_without_depends_on() -> None:
    cons: Any = _FakeConsumer(depends_on=())
    conn: Any = None
    assert _build_deps(cons, conn, 1, {"cons": cons}) == {}


def test_build_deps_skips_dep_without_capability() -> None:
    # la dependencia existe pero NO declara provide_domain (solo CAP_EXTRACT) → no se inyecta
    plain: Any = _FakeConsumer(depends_on=())
    cons: Any = _FakeConsumer(depends_on=("cons",))  # depende de otro consumer sin la cap
    conn: Any = None
    assert _build_deps(cons, conn, 1, {"cons": plain}) == {}


def test_build_deps_skips_cap_without_method() -> None:
    # declara la cap pero le falta el método → isinstance(DomainProvider) falla → no se inyecta
    np: Any = _CapButNoMethod()
    cons: Any = _FakeConsumer(depends_on=("fake",))
    conn: Any = None
    assert _build_deps(cons, conn, 1, {"fake": np, "cons": cons}) == {}


def test_build_deps_injects_optional_provider() -> None:
    # dependencia BLANDA: si el proveedor está activo, su handle se inyecta igual que depends_on.
    prov: Any = _FakeProvider()
    cons: Any = _FakeConsumer(optional_deps=("prov",))
    conn: Any = None
    assert _build_deps(cons, conn, 9, {"prov": prov, "cons": cons}) == {"prov": ("handle", 9)}


def test_build_deps_optional_provider_absent_is_empty() -> None:
    # optional dep con proveedor NO activo → no aparece en ctx.deps (el módulo cae a best-effort).
    cons: Any = _FakeConsumer(optional_deps=("prov",))
    conn: Any = None
    assert _build_deps(cons, conn, 1, {"cons": cons}) == {}


def test_identidades_provide_domain_resolves() -> None:
    # el handle REAL que entrega el módulo resuelve una identidad sembrada (lo que recibiría un
    # módulo dependiente vía ctx.deps["identidades"]).
    with connection() as c:
        oid = int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name) "
                    "VALUES (1,'organizacion','Universidad del Norte') RETURNING id"
                )
            ).scalar_one()
        )
        handle = IdentidadesModule().provide_domain(c, 1)
        assert isinstance(handle, IdentidadesDomainReader)
        hit = handle.resolve(name="Universidad del Norte")
        assert hit is not None
        assert hit.id == oid
        assert hit.kind == "organizacion"
