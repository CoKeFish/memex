"""Configuración del asistente (env `MEMEX_ASSISTANT_*`, `.env` del directorio de trabajo).

Diseñada para correr igual en el PC y en un VPS: transporte `polling` (sin puerto público) o
`webhook`; transcripción `openai` (API, sin GPU ni RAM extra) o `local` (faster-whisper).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AssistantSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMEX_ASSISTANT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    #: Token de BotFather. Sin token el bot no arranca (`memex-assistant doctor` lo dice).
    telegram_bot_token: str = ""
    #: Ids de usuario de Telegram autorizados (coma-separados). Vacío = nadie puede hablarle.
    allowed_user_ids: str = ""
    #: `polling` pregunta a Telegram (funciona detrás de NAT, sin URL pública); `webhook` exige
    #: `webhook_url` pública y un puerto abierto (opción para el VPS).
    transport: Literal["polling", "webhook"] = "polling"
    webhook_url: str = ""
    webhook_listen: str = "0.0.0.0"
    webhook_port: int = 8788
    webhook_secret: str = ""

    # --- Transcripción de audio (seam `memex.assistant.stt`) ---
    stt: Literal["openai", "local", "off"] = "openai"
    stt_language: str = "es"
    stt_openai_model: str = "gpt-4o-mini-transcribe"
    stt_openai_base_url: str = "https://api.openai.com/v1"
    stt_local_model: str = "turbo"
    stt_local_device: str = "cpu"
    stt_local_compute_type: str = "int8"
    #: La llave de OpenAI se lee de `OPENAI_API_KEY` (o `OCR_API_KEY`, la del OCR), sin prefijo.
    openai_api_key: str = Field(
        default="", validation_alias=AliasChoices("OPENAI_API_KEY", "OCR_API_KEY")
    )

    # --- Cerebro ---
    #: `api` = chat con herramientas y STREAMING por API directa (DeepSeek por default): 2-5 s por
    #: turno. `codex` = el agente `codex exec` (p50 31 s medido 2026-09-09); con `api`, codex
    #: queda para las fotos (DeepSeek no ve imágenes) y como fallback si la API falla.
    brain: Literal["api", "codex"] = "api"
    #: Override del proveedor/modelo del cerebro `api`. Vacío = `llm_consumer_settings`
    #: (consumer `assistant_brain`; `memex-llm settings set assistant_brain --provider …`) y, sin
    #: fila, DeepSeek con su modelo default. Solo proveedores con herramientas+streaming
    #: (deepseek | openai).
    brain_provider: str = ""
    brain_model: str = ""
    #: Idas y vueltas máximas con las herramientas en un turno.
    brain_max_tool_rounds: int = 6
    #: Cada cuánto se edita el mensaje de Telegram con el texto parcial (la API limita ediciones).
    stream_edit_interval_s: float = 1.2

    # --- codex (fotos, fallback, o todo el cerebro con brain=codex) ---
    codex_model: str = ""
    codex_timeout_s: float = 300.0
    #: Ante cuota/ocupado: se avisa en el chat y se reintenta cada `codex_wait_s` hasta
    #: `codex_wait_max_s` (regla del proyecto: con cuota se ESPERA, no se degrada).
    codex_wait_s: float = 300.0
    codex_wait_max_s: float = 6 * 3600.0
    #: Turnos (usuario+asistente) que se le muestran al modelo como historial del chat.
    history_turns: int = 8

    # --- escrituras sin confirmación ---
    #: Escrituras `<dominio> <verbo>` (separadas por coma) que el sistema ejecuta APENAS el cerebro
    #: las propone, sin botón de confirmación. Default: registrar un gasto/ingreso — el dueño pidió
    #: (2026-09-15) que una factura/recibo se registre «y ya», sin esperarlo; corregir un registro
    #: cuesta menos que confirmarlos todos. Vacío = todo pide confirmación.
    auto_execute: str = "finance register"

    # --- memex ---
    #: Cómo invoca el agente al CLI. Default: el Python de este venv con `-m memex.agent_cli`,
    #: que funciona igual en el host y en el contenedor (la imagen no instala console scripts).
    memex_command: str = ""
    #: Directorio de trabajo de los comandos (`.env` de memex vive ahí). Default: el actual.
    workdir: str = ""
    #: Dónde se guardan fotos/audios recibidos (se borran tras el turno). Default: temp del sistema.
    media_dir: str = ""

    @property
    def allowed_ids(self) -> frozenset[int]:
        return frozenset(
            int(p) for p in self.allowed_user_ids.replace(";", ",").split(",") if p.strip()
        )

    @property
    def auto_execute_prefixes(self) -> tuple[str, ...]:
        return tuple(
            " ".join(p.split()) for p in self.auto_execute.replace(";", ",").split(",") if p.strip()
        )

    @property
    def memex_argv(self) -> list[str]:
        if self.memex_command.strip():
            return self.memex_command.split()
        return [sys.executable, "-m", "memex.agent_cli"]

    @property
    def workdir_path(self) -> Path:
        return Path(self.workdir) if self.workdir else Path.cwd()

    @property
    def media_path(self) -> Path:
        return (
            Path(self.media_dir)
            if self.media_dir
            else Path(tempfile.gettempdir()) / "memex-assistant"
        )
