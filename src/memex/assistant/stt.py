"""Transcripción de notas de voz (speech-to-text) detrás de un seam.

- `OpenAITranscriber`: API `/audio/transcriptions` (gpt-4o-mini-transcribe / whisper-1). Acepta
  OGG/Opus (el formato de las notas de voz de Telegram) directo. Sirve en un VPS sin GPU ni RAM
  extra; ~USD 0,003/min.
- `LocalWhisperTranscriber`: faster-whisper (extra `stt-local`), modelo `turbo` en CPU int8:
  verificado en el PC del usuario (nota de 12 s en español → transcripción exacta en 3,5 s).
  El modelo se carga una vez (lazy) y en un hilo para no bloquear el loop.
- `DisabledTranscriber`: el asistente avisa que no acepta audio.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

import httpx

from memex.assistant.config import AssistantSettings
from memex.logging import get_logger

_log = get_logger("memex.assistant.stt")

_MIME_BY_SUFFIX = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
}


class SttError(RuntimeError):
    """La transcripción falló o está deshabilitada; el mensaje es apto para mostrarle al usuario."""


class Transcriber(Protocol):
    name: str

    async def transcribe(self, path: Path) -> str: ...


class OpenAITranscriber:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-4o-mini-transcribe",
        language: str = "es",
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 60.0,
    ) -> None:
        if not api_key:
            raise SttError("transcripción por OpenAI sin OPENAI_API_KEY")
        self._api_key = api_key
        self._model = model
        self._language = language
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def transcribe(self, path: Path) -> str:
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
        data = {"model": self._model, "language": self._language, "response_format": "json"}
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(
                f"{self._base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=data,
                files={"file": (path.name, path.read_bytes(), mime)},
            )
        if resp.status_code >= 400:
            _log.warning(
                "assistant.stt.openai_failed", status=resp.status_code, body=resp.text[:200]
            )
            raise SttError(f"la API de transcripción respondió {resp.status_code}")
        body: dict[str, Any] = resp.json()
        text = str(body.get("text", "")).strip()
        _log.info("assistant.stt.done", provider=self.name, chars=len(text))
        return text


class LocalWhisperTranscriber:
    name = "local"

    def __init__(
        self,
        *,
        model: str = "turbo",
        language: str = "es",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self._model_name = model
        self._language = language
        self._device = device
        self._compute_type = compute_type
        self._model: Any = None
        self._lock = asyncio.Lock()

    def _load(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:  # pragma: no cover - depende del extra instalado
            raise SttError(
                "faster-whisper no está instalado (uv sync --extra stt-local) o usá STT=openai"
            ) from e
        return WhisperModel(self._model_name, device=self._device, compute_type=self._compute_type)

    def _run(self, path: Path) -> str:
        segments, _info = self._model.transcribe(
            str(path), language=self._language, beam_size=5, vad_filter=True
        )
        return " ".join(str(s.text).strip() for s in segments).strip()

    async def transcribe(self, path: Path) -> str:
        async with self._lock:  # un modelo, una transcripción a la vez
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)
                _log.info("assistant.stt.local_loaded", model=self._model_name, device=self._device)
            text = await asyncio.to_thread(self._run, path)
        _log.info("assistant.stt.done", provider=self.name, chars=len(text))
        return text


class DisabledTranscriber:
    name = "off"

    async def transcribe(self, path: Path) -> str:
        raise SttError(
            "este asistente no tiene transcripción de audio configurada (MEMEX_ASSISTANT_STT)"
        )


def build_transcriber(settings: AssistantSettings) -> Transcriber:
    if settings.stt == "openai":
        return OpenAITranscriber(
            settings.openai_api_key,
            model=settings.stt_openai_model,
            language=settings.stt_language,
            base_url=settings.stt_openai_base_url,
        )
    if settings.stt == "local":
        return LocalWhisperTranscriber(
            model=settings.stt_local_model,
            language=settings.stt_language,
            device=settings.stt_local_device,
            compute_type=settings.stt_local_compute_type,
        )
    return DisabledTranscriber()
