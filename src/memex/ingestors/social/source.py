"""Sources sociales — implementan `Source[SocialCursor]` (polling) sobre Apify.

Tres clases (`InstagramSource`, `FacebookSource`, `XSource`), una por plataforma.
Comparten el cliente Apify, el `SocialPostPayload`, el `SocialCursor` y toda la
orquestación (`_common.social_fetch` / `advance_social_checkpoint` /
`social_health_probe`). Cada una solo aporta: su `type`, su parser de items y su
builder de run-input (el shape que el actor de esa plataforma espera).

Cumplen el contrato `Source[CursorT]`:
- `kind = SourceKind.SOCIAL`, `payload_schema = SocialPostPayload`,
  `config_schema = SocialConfig`, `checkpoint_schema = SocialCursor`.
- `fetch(checkpoint: SocialCursor) -> Iterable[SourceRecord]` — generador sync.
- `advance_checkpoint` lee `{platform}:{account}:{post_id}` del `external_id`.
- `async health_check()` valida el token de Apify (never raises).
"""

from __future__ import annotations

from builtins import type as _type
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from pydantic import BaseModel

from memex.core.cursors import SocialCursor
from memex.core.payloads import BasePayload, SocialPostPayload
from memex.core.source import ActorRunReport, HealthResult, Source, SourceKind, SourceRecord
from memex.ingestors.social._common import (
    RunWindow,
    advance_social_checkpoint,
    social_fetch,
    social_health_probe,
)
from memex.ingestors.social.config import SocialConfig
from memex.ingestors.social.parser import (
    parse_facebook_item,
    parse_instagram_item,
    parse_x_item,
)
from memex.logging import get_logger


def _instagram_run_input(account: str, window: RunWindow) -> dict[str, Any]:
    run_input: dict[str, Any] = {
        "directUrls": [f"https://www.instagram.com/{account}/"],
        "resultsType": "posts",
        "resultsLimit": window.limit,
    }
    # Cota inferior nativa (UTC, precisión de día). El actor NO tiene techo de fecha: el
    # `until` del rango lo aplica el backstop client-side de _common (escanea desde hoy
    # hacia atrás igual — el freno de costo es `resultsLimit`).
    if window.since is not None:
        run_input["onlyPostsNewerThan"] = window.since.isoformat()
    return run_input


def _facebook_run_input(account: str, window: RunWindow) -> dict[str, Any]:
    run_input: dict[str, Any] = {
        "startUrls": [{"url": f"https://www.facebook.com/{account}"}],
        "resultsLimit": window.limit,
    }
    # Ojo costo: usar filtro de fecha activa el add-on por post del actor de FB.
    if window.since is not None:
        run_input["onlyPostsNewerThan"] = window.since.isoformat()
    if window.until is not None:
        run_input["onlyPostsOlderThan"] = window.until.isoformat()
    return run_input


def _x_run_input(account: str, window: RunWindow) -> dict[str, Any]:
    run_input: dict[str, Any] = {
        "twitterHandles": [account],
        "maxItems": window.limit,
        "sort": "Latest",
    }
    if window.since is not None:
        run_input["start"] = window.since.isoformat()
    if window.until is not None:
        run_input["end"] = window.until.isoformat()
    return run_input


class InstagramSource:
    """Polling Source para posts públicos de Instagram vía Apify."""

    type: ClassVar[str] = "instagram"
    kind: ClassVar[SourceKind] = SourceKind.SOCIAL
    payload_schema: ClassVar[_type[BasePayload]] = SocialPostPayload
    config_schema: ClassVar[_type[BaseModel]] = SocialConfig
    checkpoint_schema: ClassVar[_type[BaseModel]] = SocialCursor

    def __init__(self, cfg: SocialConfig) -> None:
        self.cfg = cfg
        self._run_reports: list[ActorRunReport] = []
        self._log = get_logger("memex.ingestors.social.source", platform="instagram")

    async def health_check(self) -> HealthResult:
        return await social_health_probe(self.cfg)

    def fetch(self, checkpoint: SocialCursor) -> Iterable[SourceRecord]:
        yield from social_fetch(
            self.cfg,
            checkpoint,
            parse_item=parse_instagram_item,
            build_run_input=_instagram_run_input,
            log=self._log,
            reports=self._run_reports,
        )

    def pop_run_reports(self) -> list[ActorRunReport]:
        """Drena los reports de runs de actor acumulados por fetch() (`ActorRunReporting`)."""
        out, self._run_reports = self._run_reports, []
        return out

    def advance_checkpoint(self, checkpoint: SocialCursor, last: SourceRecord) -> SocialCursor:
        return advance_social_checkpoint(checkpoint, last)


class FacebookSource:
    """Polling Source para posts públicos de Facebook Pages vía Apify."""

    type: ClassVar[str] = "facebook"
    kind: ClassVar[SourceKind] = SourceKind.SOCIAL
    payload_schema: ClassVar[_type[BasePayload]] = SocialPostPayload
    config_schema: ClassVar[_type[BaseModel]] = SocialConfig
    checkpoint_schema: ClassVar[_type[BaseModel]] = SocialCursor

    def __init__(self, cfg: SocialConfig) -> None:
        self.cfg = cfg
        self._run_reports: list[ActorRunReport] = []
        self._log = get_logger("memex.ingestors.social.source", platform="facebook")

    async def health_check(self) -> HealthResult:
        return await social_health_probe(self.cfg)

    def fetch(self, checkpoint: SocialCursor) -> Iterable[SourceRecord]:
        yield from social_fetch(
            self.cfg,
            checkpoint,
            parse_item=parse_facebook_item,
            build_run_input=_facebook_run_input,
            log=self._log,
            reports=self._run_reports,
        )

    def pop_run_reports(self) -> list[ActorRunReport]:
        """Drena los reports de runs de actor acumulados por fetch() (`ActorRunReporting`)."""
        out, self._run_reports = self._run_reports, []
        return out

    def advance_checkpoint(self, checkpoint: SocialCursor, last: SourceRecord) -> SocialCursor:
        return advance_social_checkpoint(checkpoint, last)


class XSource:
    """Polling Source para posts públicos de X (Twitter) vía Apify."""

    type: ClassVar[str] = "x"
    kind: ClassVar[SourceKind] = SourceKind.SOCIAL
    payload_schema: ClassVar[_type[BasePayload]] = SocialPostPayload
    config_schema: ClassVar[_type[BaseModel]] = SocialConfig
    checkpoint_schema: ClassVar[_type[BaseModel]] = SocialCursor

    def __init__(self, cfg: SocialConfig) -> None:
        self.cfg = cfg
        self._run_reports: list[ActorRunReport] = []
        self._log = get_logger("memex.ingestors.social.source", platform="x")

    async def health_check(self) -> HealthResult:
        return await social_health_probe(self.cfg)

    def fetch(self, checkpoint: SocialCursor) -> Iterable[SourceRecord]:
        yield from social_fetch(
            self.cfg,
            checkpoint,
            parse_item=parse_x_item,
            build_run_input=_x_run_input,
            log=self._log,
            reports=self._run_reports,
        )

    def pop_run_reports(self) -> list[ActorRunReport]:
        """Drena los reports de runs de actor acumulados por fetch() (`ActorRunReporting`)."""
        out, self._run_reports = self._run_reports, []
        return out

    def advance_checkpoint(self, checkpoint: SocialCursor, last: SourceRecord) -> SocialCursor:
        return advance_social_checkpoint(checkpoint, last)


def make_instagram_source(cfg: dict[str, Any], env: Mapping[str, str] | None = None) -> Source[Any]:
    """SourceFactory para Instagram — valida config dict y retorna `InstagramSource`."""
    return InstagramSource(SocialConfig.from_source_config(cfg, env, platform="instagram"))


def make_facebook_source(cfg: dict[str, Any], env: Mapping[str, str] | None = None) -> Source[Any]:
    """SourceFactory para Facebook — valida config dict y retorna `FacebookSource`."""
    return FacebookSource(SocialConfig.from_source_config(cfg, env, platform="facebook"))


def make_x_source(cfg: dict[str, Any], env: Mapping[str, str] | None = None) -> Source[Any]:
    """SourceFactory para X — valida config dict y retorna `XSource`."""
    return XSource(SocialConfig.from_source_config(cfg, env, platform="x"))
