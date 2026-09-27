"""Parseo de la configuración del asistente."""

from __future__ import annotations

from memex.assistant.config import AssistantSettings


def test_auto_execute_default_and_parsing() -> None:
    assert AssistantSettings().auto_execute_prefixes == ("finance register",)
    s = AssistantSettings(auto_execute=" finance  register , bienestar register;calendario add ")
    assert s.auto_execute_prefixes == ("finance register", "bienestar register", "calendario add")
    assert AssistantSettings(auto_execute="").auto_execute_prefixes == ()
