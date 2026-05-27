"""Tests del plugin bundled imap-university."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "imap-university"


@pytest.fixture(scope="module")
def plugin_module():
    spec = importlib.util.spec_from_file_location(
        "_test_imap_university", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_imap_university"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_plugin_attributes(plugin_module) -> None:
    assert plugin_module.name == "imap-university"
    assert plugin_module.source_type == "imap"
    assert plugin_module.default_schedule.startswith("PT")


def test_validate_requirements_flags_missing_envvars(
    plugin_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UNI_IMAP_USER", raising=False)
    monkeypatch.delenv("UNI_IMAP_PASS", raising=False)
    cfg = {
        "server": "imap.test.edu",
        "auth": "basic",
        "username_env": "UNI_IMAP_USER",
        "password_env": "UNI_IMAP_PASS",
        "folders": ["INBOX"],
    }
    problems = plugin_module.validate_requirements(cfg)
    codes = {p.code for p in problems}
    assert "env-empty" in codes


def test_validate_requirements_passes_with_envvars(
    plugin_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNI_IMAP_USER", "alumno@uni.edu")
    monkeypatch.setenv("UNI_IMAP_PASS", "p")
    cfg = {
        "server": "imap.test.edu",
        "auth": "basic",
        "username_env": "UNI_IMAP_USER",
        "password_env": "UNI_IMAP_PASS",
        "folders": ["INBOX"],
    }
    assert plugin_module.validate_requirements(cfg) == []


def test_build_source_returns_imap_source(plugin_module, monkeypatch: pytest.MonkeyPatch) -> None:
    from memex.ingestors.imap.source import ImapSource

    monkeypatch.setenv("UNI_IMAP_USER", "alumno@uni.edu")
    monkeypatch.setenv("UNI_IMAP_PASS", "p")
    cfg = {
        "server": "imap.test.edu",
        "auth": "basic",
        "username_env": "UNI_IMAP_USER",
        "password_env": "UNI_IMAP_PASS",
        "folders": ["INBOX"],
    }
    src = plugin_module.build_source(cfg)
    assert isinstance(src, ImapSource)
