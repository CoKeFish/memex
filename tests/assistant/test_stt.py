"""Transcripción: la API de OpenAI con respx (sin red), el seam `build_transcriber` y `off`."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from memex.assistant.config import AssistantSettings
from memex.assistant.stt import (
    DisabledTranscriber,
    LocalWhisperTranscriber,
    OpenAITranscriber,
    SttError,
    build_transcriber,
)


@pytest.mark.asyncio
@respx.mock
async def test_openai_transcriber_posts_multipart_and_returns_text(tmp_path: Path) -> None:
    audio = tmp_path / "nota.ogg"
    audio.write_bytes(b"OggS-fake")
    route = respx.post("https://api.openai.com/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "  Hola memex, pagué 25 mil de Uber. "})
    )
    stt = OpenAITranscriber("sk-test", model="gpt-4o-mini-transcribe", language="es")
    assert await stt.transcribe(audio) == "Hola memex, pagué 25 mil de Uber."
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer sk-test"
    body = req.content
    assert b'name="model"' in body and b"gpt-4o-mini-transcribe" in body
    assert b'name="language"' in body and b"audio/ogg" in body and b"OggS-fake" in body


@pytest.mark.asyncio
@respx.mock
async def test_openai_transcriber_error_is_stt_error(tmp_path: Path) -> None:
    audio = tmp_path / "nota.ogg"
    audio.write_bytes(b"x")
    respx.post("https://api.openai.com/v1/audio/transcriptions").mock(
        return_value=httpx.Response(429, json={"error": "rate"})
    )
    with pytest.raises(SttError, match="429"):
        await OpenAITranscriber("sk-test").transcribe(audio)


def test_build_transcriber_by_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert isinstance(build_transcriber(AssistantSettings(stt="openai")), OpenAITranscriber)
    assert isinstance(build_transcriber(AssistantSettings(stt="local")), LocalWhisperTranscriber)
    assert isinstance(build_transcriber(AssistantSettings(stt="off")), DisabledTranscriber)
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("OCR_API_KEY", raising=False)
    with pytest.raises(SttError, match="OPENAI_API_KEY"):
        build_transcriber(AssistantSettings(stt="openai", _env_file=None))


@pytest.mark.asyncio
async def test_disabled_transcriber_raises() -> None:
    with pytest.raises(SttError):
        await DisabledTranscriber().transcribe(Path("x.ogg"))


def test_settings_allowlist_and_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    s = AssistantSettings(allowed_user_ids="12, 34;56", memex_command="", _env_file=None)
    assert s.allowed_ids == frozenset({12, 34, 56})
    assert s.memex_argv[-2:] == ["-m", "memex.agent_cli"]
    assert AssistantSettings(memex_command="uv run memex", _env_file=None).memex_argv == [
        "uv",
        "run",
        "memex",
    ]
