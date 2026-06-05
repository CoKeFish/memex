"""Registry + disciplina de Protocol del módulo identidades, y el handle `provide_domain`."""

from __future__ import annotations

from sqlalchemy import text

from memex.core.source import SourceKind
from memex.db import connection
from memex.modules import known_modules, resolve
from memex.modules.contract import CAP_EXTRACT, CAP_PROVIDE_DOMAIN, InterestModule
from memex.modules.identidades.domain import IdentidadesDomain, IdentidadesDomainReader
from memex.modules.identidades.module import IdentidadesModule


def test_known_modules_includes_identidades() -> None:
    assert "identidades" in known_modules()


def test_resolve_builds_module() -> None:
    assert isinstance(resolve("identidades")(), IdentidadesModule)


def test_satisfies_interest_module() -> None:
    assert isinstance(IdentidadesModule(), InterestModule)


def test_declares_capabilities() -> None:
    assert {CAP_EXTRACT, CAP_PROVIDE_DOMAIN} <= IdentidadesModule.capabilities


def test_consumes_email_chat_social() -> None:
    assert set(IdentidadesModule.consumes_kinds) == {
        SourceKind.EMAIL,
        SourceKind.CHAT,
        SourceKind.SOCIAL,
    }


def test_domain_reader_resolves() -> None:
    with connection() as c:
        pid = c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, 'persona', 'Ada Lovelace') RETURNING id"
            )
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO mod_identidades_identifiers "
                "(user_id, identity_id, platform, kind, value, value_norm) "
                "VALUES (1, :i, 'email', 'email', 'ada@x.com', 'ada@x.com')"
            ),
            {"i": pid},
        )
        oid = c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, 'organizacion', 'Unity') RETURNING id"
            )
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO mod_identidades_identifiers "
                "(user_id, identity_id, platform, kind, value, value_norm) "
                "VALUES (1, :i, 'domain', 'domain', 'unity.com', 'unity.com')"
            ),
            {"i": oid},
        )

    with connection() as conn:
        reader = IdentidadesDomainReader(conn, 1)
        assert isinstance(reader, IdentidadesDomain)
        by_email = reader.resolve(email="ada@x.com")
        assert by_email is not None and by_email.kind == "persona"
        by_domain = reader.resolve(name="Soporte", email="x@unity.com")
        assert by_domain is not None and by_domain.kind == "organizacion"
        by_name = reader.resolve(name="Unity")
        assert by_name is not None and by_name.kind == "organizacion"
        assert reader.resolve(name="Nadie Conocido") is None
