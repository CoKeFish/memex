"""Configuración de una fuente Notion (`sources.config` → `NotionConfig`).

`roots` son los ids (UUID, con o sin guiones, o la URL de la página) de las páginas RAÍZ que el
usuario eligió: solo ellas y sus subpáginas entran. La integración interna de Notion solo ve lo que
se le compartió, así que el límite queda en los dos lados. `roots=[]` = todo lo compartido con la
integración (opt-in explícito). El token vive en el vault de la cuenta y llega por la env var
`token_env` (default `MEMEX_NOTION_TOKEN`), igual que el resto de ingestores.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from memex.core.source import SourceConfigError

DEFAULT_TOKEN_ENV = "MEMEX_NOTION_TOKEN"
#: Versión de la API de Notion que este ingestor habla (pages/blocks/search clásicos).
NOTION_VERSION = "2022-06-28"
_UUID_RX = re.compile(r"([0-9a-fA-F]{32})")


class NotionConfigError(SourceConfigError):
    """Config de Notion inválida (token ausente, raíz malformada)."""


class NotionConfig(BaseModel):
    token: SecretStr
    #: Ids de página raíz normalizados (UUID con guiones). Vacío = todo lo compartido.
    roots: list[str] = Field(default_factory=list)
    #: Profundidad máxima de subpáginas bajo una raíz (0 = solo la raíz).
    max_depth: int = 6
    #: Páginas (nuevas o editadas) que se releen por corrida; el resto queda para la siguiente.
    max_pages_per_run: int = 100
    #: Tope de bloques por página al volcar el texto (después se marca `body_truncated`).
    max_blocks_per_page: int = 2000
    #: Holgura al comparar contra la marca de agua (relojes/propagación de Notion).
    watermark_slack_s: int = 120
    #: Llamadas por segundo (Notion permite ~3).
    requests_per_second: float = 3.0
    timeout_s: float = 30.0
    notion_version: str = NOTION_VERSION

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def from_source_config(
        cls, cfg: dict[str, Any], env: Mapping[str, str] | None = None
    ) -> NotionConfig:
        env_map: Mapping[str, str] = env if env is not None else os.environ
        token_env = str(cfg.get("token_env") or DEFAULT_TOKEN_ENV)
        token = _require_env(env_map, token_env)
        raw_roots = cfg.get("roots", [])
        if not isinstance(raw_roots, list):
            raise NotionConfigError("'roots' must be a list of page ids or URLs")
        roots: list[str] = []
        for i, r in enumerate(raw_roots):
            nid = normalize_page_id(str(r))
            if nid is None:
                raise NotionConfigError(f"'roots[{i}]' is not a Notion page id or URL: {r!r}")
            if nid not in roots:
                roots.append(nid)
        try:
            return cls(
                token=SecretStr(token),
                roots=roots,
                max_depth=int(cfg.get("max_depth", 6)),
                max_pages_per_run=int(cfg.get("max_pages_per_run", 100)),
                max_blocks_per_page=int(cfg.get("max_blocks_per_page", 2000)),
                watermark_slack_s=int(cfg.get("watermark_slack_s", 120)),
                requests_per_second=float(cfg.get("requests_per_second", 3.0)),
                timeout_s=float(cfg.get("timeout_s", 30.0)),
                notion_version=str(cfg.get("notion_version") or NOTION_VERSION),
            )
        except (TypeError, ValueError) as e:
            raise NotionConfigError(f"invalid notion config: {e}") from e


def normalize_page_id(value: str) -> str | None:
    """`https://www.notion.so/Mi-pagina-0123…abcd`, `0123…abcd` o el UUID con guiones → UUID con
    guiones en minúsculas. None si no hay 32 hex adentro."""
    raw = value.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    compact = raw.rsplit("/", 1)[-1].replace("-", "")
    m = re.search(r"([0-9a-fA-F]{32})$", compact) or _UUID_RX.search(compact)
    if m is None:
        return None
    h = m.group(1).lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _require_env(env_map: Mapping[str, str], var: str) -> str:
    if var not in env_map:
        raise NotionConfigError(f"env var {var!r} is not set")
    value = env_map[var].strip()
    if not value:
        raise NotionConfigError(f"env var {var!r} resolves to empty value")
    return value
