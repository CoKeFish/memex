"""SocialConfig — configuración resuelta para una source social (Apify).

Sigue la misma convención que `ImapConfig` / `TelegramConfig`:

- Pydantic `BaseModel` (frozen) — satisface `Source.config_schema: type[BaseModel]`.
- `from_source_config(cfg, env, *, platform)` resuelve el token desde una env var
  (el nombre vive en `sources.config`, el valor nunca toca la DB — ADR-001).
- `__repr__` custom que redacta `apify_token` en logs.

`platform` la fija el factory (`make_instagram_source` / `make_facebook_source` /
`make_x_source`), NO el operador — se deriva de `source.type`, no es un campo de
config que el operador setee.

`AllowedAccount` es una entrada de la allowlist: el handle / página pública a
scrapear. `account` se normaliza (lowercase, sin `@`, sin URL) para que matchee la
key del cursor y el segmento del `external_id`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from memex.core.media_types import DEFAULT_MAX_ATTACHMENT_BYTES
from memex.core.source import SourceConfigError

Platform = Literal["instagram", "facebook", "x"]

_DEFAULT_ACTORS: dict[Platform, str] = {
    "instagram": "apify/instagram-scraper",
    "facebook": "apify/facebook-posts-scraper",
    "x": "apidojo/tweet-scraper",
}
_DEFAULT_APIFY_TOKEN_ENV = "MEMEX_APIFY_TOKEN"
#: Tope para video crudo: más alto que el de imágenes/PDF (el usuario quiere el video completo).
#: Override por-source en sources.config (`max_video_bytes`).
_DEFAULT_MAX_VIDEO_BYTES = 100 * 1024 * 1024


class SocialConfigError(SourceConfigError):
    """Raised cuando la config de una source social es inválida o falta la env var.

    Subclasea `SourceConfigError` para que los callers atrapen la base genérica y
    traten cualquier fallo de config uniformemente.
    """


class AllowedAccount(BaseModel):
    """Una entrada de la allowlist de cuentas a scrapear.

    `account` debe estar normalizado (lo hace `from_source_config`): el handle /
    nombre de página en minúsculas, sin `@` ni URL. `priority` destaca la cuenta
    (uso futuro, p. ej. ordenar o frecuencias distintas).
    """

    account: str
    priority: bool = False

    model_config = ConfigDict(frozen=True, extra="forbid")


class SocialConfig(BaseModel):
    """Configuración resuelta para una source social.

    Una `SocialConfig` = una plataforma + un set de cuentas públicas a seguir, vía
    un actor de Apify. El token de Apify es compartido (una cuenta Apify); cada
    plataforma usa su actor (`actor_id`, override-able).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: Platform
    # SecretStr redacta en repr(), str(), f-strings Y model_dump/json — no solo en
    # __repr__. Los callers usan `.get_secret_value()` en el borde del ApifyClient.
    apify_token: SecretStr
    actor_id: str

    accounts: list[AllowedAccount] = Field(default_factory=list)

    results_limit: int = 30
    run_timeout_s: int = 120

    # Extracción de media (fotos + video crudo) para MinIO + OCR. Off por default → sin cambio de
    # comportamiento. Se habilita por-source en sources.config (`extract_media: true`). Espejo de
    # `ImapConfig.extract_media`. Las imágenes alimentan OCR; el video se guarda pero no se OCR-ea.
    extract_media: bool = False
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    max_video_bytes: int = _DEFAULT_MAX_VIDEO_BYTES

    # Carry el *nombre* de la env var (no el valor) para logging / debugging.
    apify_token_env: str = ""

    def __repr__(self) -> str:
        return (
            "SocialConfig("
            f"platform={self.platform!r}, "
            "apify_token=<redacted>, "
            f"actor_id={self.actor_id!r}, "
            f"accounts={len(self.accounts)} entries, "
            f"results_limit={self.results_limit}, "
            f"run_timeout_s={self.run_timeout_s}, "
            f"extract_media={self.extract_media}, "
            f"max_attachment_bytes={self.max_attachment_bytes}, "
            f"max_video_bytes={self.max_video_bytes})"
        )

    @classmethod
    def from_source_config(
        cls,
        cfg: dict[str, Any],
        env: Mapping[str, str] | None = None,
        *,
        platform: Platform,
    ) -> SocialConfig:
        """Resuelve la env var del token y construye una `SocialConfig` validada.

        Espera en `cfg`:
        - `apify_token_env` (default `MEMEX_APIFY_TOKEN`) — nombre de la env var
          que contiene el token de Apify.
        - `actor_id` opcional — override del actor por defecto de la plataforma.
        - `accounts`: lista de dicts con `account` (handle/URL/página) y opcional
          `priority`.
        - `results_limit` opcional (default 30) — posts a scrapear por cuenta/run.
        - `run_timeout_s` opcional (default 120) — timeout del run del actor.
        - `extract_media` opcional (default False) — bajar fotos + video crudo a MinIO + OCR.
        - `max_attachment_bytes` / `max_video_bytes` opcionales — topes por asset (foto / video).
        """
        env_map: Mapping[str, str] = env if env is not None else os.environ

        token_env = str(cfg.get("apify_token_env") or _DEFAULT_APIFY_TOKEN_ENV)
        token_value = _require_env(env_map, token_env)

        actor_id = str(cfg.get("actor_id") or _DEFAULT_ACTORS[platform]).strip()
        if not actor_id:
            raise SocialConfigError("'actor_id' must be non-empty")

        accounts_raw = cfg.get("accounts", [])
        if not isinstance(accounts_raw, list):
            raise SocialConfigError("'accounts' must be a list of objects")
        accounts: list[AllowedAccount] = []
        for i, entry in enumerate(accounts_raw):
            if not isinstance(entry, dict):
                raise SocialConfigError(
                    f"'accounts[{i}]' must be an object, got {type(entry).__name__}"
                )
            try:
                parsed = AllowedAccount.model_validate(entry)
            except Exception as e:
                raise SocialConfigError(f"'accounts[{i}]' invalid: {e}") from e
            normalized = _normalize_account(parsed.account)
            if not normalized:
                raise SocialConfigError(f"'accounts[{i}].account' is empty after normalization")
            accounts.append(AllowedAccount(account=normalized, priority=parsed.priority))

        results_limit = int(cfg.get("results_limit", 30))
        if results_limit <= 0:
            raise SocialConfigError("'results_limit' must be positive")

        run_timeout_s = int(cfg.get("run_timeout_s", 120))
        if run_timeout_s <= 0:
            raise SocialConfigError("'run_timeout_s' must be positive")

        max_attachment_bytes = int(cfg.get("max_attachment_bytes", DEFAULT_MAX_ATTACHMENT_BYTES))
        if max_attachment_bytes <= 0:
            raise SocialConfigError("'max_attachment_bytes' must be positive")
        max_video_bytes = int(cfg.get("max_video_bytes", _DEFAULT_MAX_VIDEO_BYTES))
        if max_video_bytes <= 0:
            raise SocialConfigError("'max_video_bytes' must be positive")

        return cls(
            platform=platform,
            apify_token=SecretStr(token_value),
            actor_id=actor_id,
            accounts=accounts,
            results_limit=results_limit,
            run_timeout_s=run_timeout_s,
            extract_media=bool(cfg.get("extract_media", False)),
            max_attachment_bytes=max_attachment_bytes,
            max_video_bytes=max_video_bytes,
            apify_token_env=token_env,
        )


def normalize_account(raw: str) -> str:
    """Normaliza un identificador de cuenta a su handle canónico en minúsculas.

    Acepta handles (`@utnfrba`), nombres de página y URLs completas
    (`https://www.instagram.com/utn.frba/`) y devuelve el último segmento sin `@`
    ni barras, en minúsculas. Defensivo, no exhaustivo: para casos raros (ej.
    `facebook.com/profile.php?id=123`) el operador puede pasar el handle directo.

    Público: lo reusa la API (`/sources/{id}/social/accounts`) para que la key del
    allowlist matchee la del cursor — la MISMA normalización que `from_source_config`.
    """
    s = raw.strip()
    if "://" in s or s.startswith("www."):
        s = s.split("?", 1)[0].rstrip("/")
        s = s.rsplit("/", 1)[-1]
    return s.lstrip("@").strip().lower()


# Alias privado retro-compatible (el constructor de config lo usa internamente).
_normalize_account = normalize_account


def _require_env(env_map: Mapping[str, str], var: str) -> str:
    if var not in env_map:
        raise SocialConfigError(f"env var {var!r} is not set")
    value = env_map[var].strip()
    if not value:
        raise SocialConfigError(f"env var {var!r} resolves to empty value")
    return value
