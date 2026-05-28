"""TelegramConfig — configuración resuelta para una source de Telegram.

Sigue la misma convención que `ImapConfig`:

- Pydantic `BaseModel` (frozen) — satisface `Source.config_schema: type[BaseModel]`.
- `from_source_config(cfg, env)` resuelve secretos desde env vars (los nombres
  viven en `sources.config`, los valores nunca tocan la DB — ADR-001).
- `__repr__` custom para redact de `api_hash` y `phone` en logs.

`AllowedChat` representa una entrada de la allowlist; encapsula los matices
que el repo de referencia `CoKeFish/ingestors/telegram` resolvió en su
`telegram_allowlist` table: filtro por `chat_id`, opcionalmente acotado a
`topic_ids` específicos dentro de supergrupos con foros, marca `priority`
para destacar (uso futuro), y `streaming` para enrutar al
`TelegramStreamingSource` (Fase 3) en vez de al poller.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from memex.core.source import SourceConfigError

# session_name se compone en un path filesystem (Telethon append .session).
# Rechazamos cualquier separador, dot o char especial para evitar path
# traversal del estilo "../../etc/shadow". Aceptamos alfanumérico + _ -.
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class TelegramConfigError(SourceConfigError):
    """Raised when a Telegram source config is invalid or env vars are missing.

    Subclasses `SourceConfigError` so callers can catch the generic base and
    handle any source's config failure uniformly.
    """


class AllowedChat(BaseModel):
    """Una entrada de la allowlist de chats.

    `chat_id` debe estar en el "marked format" estable de Telethon (el que
    devuelve `telethon.utils.get_peer_id` — `-(1e12 + id)` para supergrupos y
    canales, `-<id>` para grupos básicos). `topic_ids=None` significa
    "todos los topics del chat"; lista vacía significa "ningún topic" (es
    decir, solo mensajes del top-level del chat, sin foro). `streaming=True`
    excluye el chat del polling — lo procesa el listener event-driven de
    Fase 3.
    """

    chat_id: int
    topic_ids: list[int] | None = None
    streaming: bool = False
    priority: bool = False

    model_config = ConfigDict(frozen=True, extra="forbid")

    def matches_topic(self, topic_id: int | None) -> bool:
        """True si este chat acepta mensajes de ese topic.

        - `topic_ids=None` acepta cualquier topic (y mensajes sin topic).
        - `topic_ids=[]` solo acepta mensajes SIN topic (top-level).
        - `topic_ids=[1, 2]` acepta solo esos topics.
        """
        if self.topic_ids is None:
            return True
        if topic_id is None:
            return len(self.topic_ids) == 0
        return topic_id in self.topic_ids


class TelegramConfig(BaseModel):
    """Configuración resuelta para una source de Telegram.

    Una `TelegramConfig` = una cuenta de Telegram (un session file). Los
    chats que se ingestan se controlan vía `allowed_chats`; los que tengan
    `streaming=True` quedan fuera del polling (los maneja Fase 3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_id: int
    api_hash: str
    phone: str
    session_path: Path
    session_name: str = "default"

    allowed_chats: list[AllowedChat] = Field(default_factory=list)

    batch_size: int = 100

    # Carry env-var *names* (not values) for logging / debugging.
    api_id_env: str = ""
    api_hash_env: str = ""
    phone_env: str = ""

    @property
    def phone_masked(self) -> str:
        """Phone con la mayoría de dígitos redactados — para logs."""
        if len(self.phone) <= 4:
            return "***"
        return f"{self.phone[:3]}***{self.phone[-2:]}"

    @property
    def session_file(self) -> Path:
        """Path final que se pasa a `TelegramClient(session)`.

        Telethon le agrega `.session` automáticamente, así que devolvemos sin
        extensión.
        """
        return self.session_path / self.session_name

    def __repr__(self) -> str:
        return (
            "TelegramConfig("
            f"api_id={self.api_id}, "
            "api_hash=<redacted>, "
            f"phone={self.phone_masked!r}, "
            f"session_path={self.session_path!r}, "
            f"session_name={self.session_name!r}, "
            f"allowed_chats={len(self.allowed_chats)} entries, "
            f"batch_size={self.batch_size})"
        )

    @classmethod
    def from_source_config(
        cls,
        cfg: dict[str, Any],
        env: Mapping[str, str] | None = None,
    ) -> TelegramConfig:
        """Resuelve env vars y construye una `TelegramConfig` validada.

        Espera en `cfg`:
        - `api_id_env` / `api_hash_env` / `phone_env` (defaults
          `MEMEX_TG_API_ID` / `MEMEX_TG_API_HASH` / `MEMEX_TG_PHONE`).
        - `session_path` (default: `/var/lib/memex/telegram`).
        - `session_name` (default: `default`).
        - `allowed_chats`: lista de dicts con `chat_id` y opcionalmente
          `topic_ids`, `streaming`, `priority`.
        - `batch_size` opcional (default 100).
        """
        env_map: Mapping[str, str] = env if env is not None else os.environ

        api_id_env = str(cfg.get("api_id_env") or "MEMEX_TG_API_ID")
        api_hash_env = str(cfg.get("api_hash_env") or "MEMEX_TG_API_HASH")
        phone_env = str(cfg.get("phone_env") or "MEMEX_TG_PHONE")

        api_id_raw = _require_env(env_map, api_id_env)
        try:
            api_id = int(api_id_raw)
        except ValueError as e:
            raise TelegramConfigError(
                f"env var {api_id_env!r} must be an integer, got {api_id_raw!r}"
            ) from e

        api_hash = _require_env(env_map, api_hash_env)
        phone = _require_env(env_map, phone_env)

        session_path_raw = str(
            cfg.get("session_path")
            or env_map.get("MEMEX_TG_SESSION_PATH", "/var/lib/memex/telegram")
        )
        # Defensa contra path traversal: rechazamos cualquier `..` en el path
        # bruto antes de resolverlo. Después resolvemos a absoluto canónico —
        # `.resolve()` colapsa `..` legales pero no aprueba los relativos
        # maliciosos que ya filtramos.
        if ".." in Path(session_path_raw).parts:
            raise TelegramConfigError("'session_path' must not contain '..' (path traversal)")
        session_path = Path(session_path_raw).resolve()
        if not session_path.is_absolute():  # pragma: no cover — resolve() siempre devuelve absoluto
            raise TelegramConfigError("'session_path' must be absolute")

        session_name = str(cfg.get("session_name", "default")).strip()
        if not session_name:
            raise TelegramConfigError("'session_name' must be non-empty")
        if not _SESSION_NAME_RE.match(session_name):
            raise TelegramConfigError(
                f"'session_name' must match {_SESSION_NAME_RE.pattern!r}; "
                "no separators, dots, or special chars allowed (path-traversal protection)"
            )

        allowed_raw = cfg.get("allowed_chats", [])
        if not isinstance(allowed_raw, list):
            raise TelegramConfigError("'allowed_chats' must be a list of objects")
        allowed_chats: list[AllowedChat] = []
        for i, entry in enumerate(allowed_raw):
            if not isinstance(entry, dict):
                raise TelegramConfigError(
                    f"'allowed_chats[{i}]' must be an object, got {type(entry).__name__}"
                )
            try:
                allowed_chats.append(AllowedChat.model_validate(entry))
            except Exception as e:
                raise TelegramConfigError(f"'allowed_chats[{i}]' invalid: {e}") from e

        batch_size = int(cfg.get("batch_size", 100))
        if batch_size <= 0:
            raise TelegramConfigError("'batch_size' must be positive")

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            session_path=session_path,
            session_name=session_name,
            allowed_chats=allowed_chats,
            batch_size=batch_size,
            api_id_env=api_id_env,
            api_hash_env=api_hash_env,
            phone_env=phone_env,
        )


def _require_env(env_map: Mapping[str, str], var: str) -> str:
    if var not in env_map:
        raise TelegramConfigError(f"env var {var!r} is not set")
    value = env_map[var].strip()
    if not value:
        raise TelegramConfigError(f"env var {var!r} resolves to empty value")
    return value
