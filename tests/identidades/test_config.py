"""ContactsSyncConfig.from_env: defaults, override por env y por arg, extra prohibido.

No hay API key acá (el token OAuth NO entra a la config; se resuelve en runtime desde el vault de la
cuenta), así que `from_env` no falla por falta de secreto.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex.modules.identidades.providers.config import ContactsSyncConfig

_DEFAULT_BASE = "https://people.googleapis.com/v1"


def test_from_env_default_base_url() -> None:
    cfg = ContactsSyncConfig.from_env(env={})
    assert cfg.base_url == _DEFAULT_BASE
    assert cfg.page_size == 1000


def test_from_env_reads_base_url_env() -> None:
    cfg = ContactsSyncConfig.from_env(env={"MEMEX_CONTACTS_BASE_URL": "https://x.test/v1"})
    assert cfg.base_url == "https://x.test/v1"


def test_explicit_base_url_wins_over_env() -> None:
    cfg = ContactsSyncConfig.from_env(
        env={"MEMEX_CONTACTS_BASE_URL": "https://env.test/v1"}, base_url="https://arg.test/v1"
    )
    assert cfg.base_url == "https://arg.test/v1"


def test_config_is_frozen_and_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        ContactsSyncConfig(base_url="https://x/v1", foo=1)  # type: ignore[call-arg]
