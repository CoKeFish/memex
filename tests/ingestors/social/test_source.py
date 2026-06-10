"""Social sources — contract, fetch ordering/filtering, advance, health.

Mockea `ApifyClient` (monkeypatch en `_common`) para no pegarle a Apify real.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from memex.core.cursors import AccountCursor, SocialCursor
from memex.core.source import HealthResult, Source, SourceKind, SourceRecord
from memex.ingestors.runner import run_ingestor
from memex.ingestors.social._common import (
    advance_social_checkpoint,
    is_new_record,
    social_fetch,
    split_social_external_id,
)
from memex.ingestors.social.apify_client import ApifyError, ApifyRunResult, ApifyTimeoutError
from memex.ingestors.social.config import AllowedAccount, SocialConfig
from memex.ingestors.social.source import (
    FacebookSource,
    InstagramSource,
    XSource,
    make_facebook_source,
    make_instagram_source,
    make_x_source,
)
from memex.logging import get_logger


def _cfg(
    accounts: list[AllowedAccount] | None = None,
    results_limit: int = 30,
    *,
    extract_media: bool = False,
    max_attachment_bytes: int = 10 * 1024 * 1024,
    max_video_bytes: int = 100 * 1024 * 1024,
) -> SocialConfig:
    return SocialConfig(
        platform="instagram",
        apify_token="tok",
        actor_id="apify/instagram-scraper",
        accounts=accounts if accounts is not None else [AllowedAccount(account="utn.frba")],
        results_limit=results_limit,
        run_timeout_s=10,
        extract_media=extract_media,
        max_attachment_bytes=max_attachment_bytes,
        max_video_bytes=max_video_bytes,
    )


def _ig(ts: str, pid: str) -> dict[str, Any]:
    return {"id": pid, "shortCode": pid, "caption": "c", "timestamp": ts, "type": "Image"}


def _ig_media(
    ts: str, pid: str, *, img: str | None = None, video: str | None = None
) -> dict[str, Any]:
    item = _ig(ts, pid)
    if img is not None:
        item["displayUrl"] = img
    if video is not None:
        item["videoUrl"] = video
        item["type"] = "Video"
    return item


def _fake_apify_returning(
    items: list[dict[str, Any]],
    *,
    usage: float | None = 0.01,
    charged_events: dict[str, int] | None = None,
) -> type:
    class _FakeApify:
        def __init__(self, token: str, **kwargs: Any) -> None:
            self.token = token

        async def __aenter__(self) -> _FakeApify:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def run_actor(self, actor_id: str, run_input: dict[str, Any]) -> ApifyRunResult:
            return ApifyRunResult(
                items=items, usage_usd=usage, run_id="R1", charged_events=charged_events
            )

    return _FakeApify


def _fake_apify_raising(exc: Exception) -> type:
    class _Fail:
        def __init__(self, token: str, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Fail:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def run_actor(self, actor_id: str, run_input: dict[str, Any]) -> ApifyRunResult:
            raise exc

    return _Fail


def _fake_apify_concurrency(tracker: dict[str, int]) -> type:
    """ApifyClient falso que registra cuántos `run_actor` corren a la vez."""

    class _Fake:
        def __init__(self, token: str, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Fake:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def run_actor(self, actor_id: str, run_input: dict[str, Any]) -> ApifyRunResult:
            tracker["cur"] += 1
            tracker["max"] = max(tracker["max"], tracker["cur"])
            await asyncio.sleep(0.01)
            tracker["cur"] -= 1
            return ApifyRunResult(items=[], usage_usd=0.0, run_id="R")

    return _Fake


# ---- contract ---- #


def test_sources_satisfy_contract() -> None:
    for cls, expected_type in (
        (InstagramSource, "instagram"),
        (FacebookSource, "facebook"),
        (XSource, "x"),
    ):
        assert cls.type == expected_type
        assert cls.kind is SourceKind.SOCIAL
        assert cls.payload_schema.__name__ == "SocialPostPayload"
        assert cls.config_schema is SocialConfig
        assert cls.checkpoint_schema is SocialCursor
    assert isinstance(InstagramSource(_cfg()), Source)


def test_make_factories_resolve_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMEX_APIFY_TOKEN", "secret")
    ig = make_instagram_source({"accounts": [{"account": "@UTN.FRBA"}]})
    fb = make_facebook_source({})
    x = make_x_source({})
    assert isinstance(ig, InstagramSource)
    assert isinstance(fb, FacebookSource)
    assert isinstance(x, XSource)
    assert ig.cfg.accounts[0].account == "utn.frba"
    assert x.cfg.actor_id == "apidojo/tweet-scraper"


# ---- fetch: ordering + filtering ---- #


def test_fetch_yields_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los actores devuelven newest-first; fetch debe yieldear oldest-first para que
    el runner avance el cursor a chunk[-1] = el más nuevo."""
    items = [
        _ig("2026-05-28T12:00:00Z", "p3"),
        _ig("2026-05-28T11:00:00Z", "p2"),
        _ig("2026-05-28T10:00:00Z", "p1"),
    ]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    recs = list(InstagramSource(_cfg()).fetch(SocialCursor()))
    assert [r.external_id for r in recs] == [
        "instagram:utn.frba:p1",
        "instagram:utn.frba:p2",
        "instagram:utn.frba:p3",
    ]


def test_fetch_filters_by_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        _ig("2026-05-28T12:00:00Z", "p3"),
        _ig("2026-05-28T11:00:00Z", "p2"),
        _ig("2026-05-28T10:00:00Z", "p1"),
    ]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    cursor = SocialCursor(
        accounts={
            "utn.frba": AccountCursor(
                last_post_id="p2", last_posted_at=datetime(2026, 5, 28, 11, 0, tzinfo=UTC)
            )
        }
    )
    recs = list(InstagramSource(_cfg()).fetch(cursor))
    assert [r.external_id for r in recs] == ["instagram:utn.frba:p3"]


def test_fetch_skips_when_no_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a: Any, **k: Any) -> None:
            called["n"] += 1

    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _Boom)
    recs = list(InstagramSource(_cfg(accounts=[])).fetch(SocialCursor()))
    assert recs == []
    assert called["n"] == 0  # cliente NUNCA se construye sin cuentas


def test_fetch_drops_unparseable_items(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_ig("2026-05-28T10:00:00Z", "ok"), {"garbage": True}]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    recs = list(InstagramSource(_cfg()).fetch(SocialCursor()))
    assert [r.external_id for r in recs] == ["instagram:utn.frba:ok"]


def test_social_fetch_skips_items_that_raise_in_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensa en profundidad: un item que hace raise el parser se loggea y se saltea,
    no tumba el run completo (los otros items se procesan)."""
    items = [{"k": "good1"}, {"k": "boom"}, {"k": "good2"}]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))

    def _parse(raw: dict[str, Any], account: str) -> SourceRecord | None:
        if raw.get("k") == "boom":
            raise ValueError("poison item")
        dt = datetime(2026, 5, 28, 10 if raw["k"] == "good1" else 11, 0, tzinfo=UTC)
        return _rec(f"instagram:{account}:{raw['k']}", dt)

    recs = list(
        social_fetch(
            _cfg(),
            SocialCursor(),
            parse_item=_parse,
            build_run_input=lambda _a, _l: {},
            log=get_logger("test"),
        )
    )
    assert [r.external_id for r in recs] == [
        "instagram:utn.frba:good1",
        "instagram:utn.frba:good2",
    ]


def test_fetch_scrapes_accounts_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Las cuentas se scrapean en paralelo (gather + semáforo), no una por una."""
    tracker = {"cur": 0, "max": 0}
    monkeypatch.setattr(
        "memex.ingestors.social._common.ApifyClient", _fake_apify_concurrency(tracker)
    )
    accounts = [AllowedAccount(account=a) for a in ("a", "b", "c")]
    list(InstagramSource(_cfg(accounts=accounts)).fetch(SocialCursor()))
    assert tracker["max"] >= 2


# ---- ActorRunReporting: reports de costo por run de actor ---- #


def test_fetch_collects_ok_report_and_pop_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_ig("2026-05-28T10:00:00Z", "p1"), _ig("2026-05-28T09:00:00Z", "p0")]
    monkeypatch.setattr(
        "memex.ingestors.social._common.ApifyClient",
        _fake_apify_returning(items, charged_events={"result": 2}),
    )
    src = InstagramSource(_cfg())
    recs = list(src.fetch(SocialCursor()))
    reports = src.pop_run_reports()
    assert len(reports) == 1
    rep = reports[0]
    assert (rep.platform, rep.account, rep.status) == ("instagram", "utn.frba", "ok")
    assert rep.actor_id == "apify/instagram-scraper"
    assert rep.apify_run_id == "R1"
    assert rep.items_scraped == 2
    assert rep.items_kept == len(recs) == 2
    assert rep.cost_usd == 0.01
    assert rep.charged_events == {"result": 2}
    # pop DRENA: un segundo drenaje no duplica filas.
    assert src.pop_run_reports() == []


def test_fetch_reports_one_per_account(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_ig("2026-05-28T10:00:00Z", "p1")]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    src = InstagramSource(_cfg(accounts=[AllowedAccount(account="a"), AllowedAccount(account="b")]))
    list(src.fetch(SocialCursor()))
    reports = src.pop_run_reports()
    assert [r.account for r in reports] == ["a", "b"]  # gather preserva el orden
    assert all(r.status == "ok" for r in reports)


def test_fetch_reports_error_run_without_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un run que falla TAMBIÉN se reporta (pudo cobrar) — sin run_id ni costo conocidos."""
    monkeypatch.setattr(
        "memex.ingestors.social._common.ApifyClient",
        _fake_apify_raising(ApifyError(500, "server error")),
    )
    src = InstagramSource(_cfg())
    assert list(src.fetch(SocialCursor())) == []
    [rep] = src.pop_run_reports()
    assert rep.status == "error"
    assert rep.apify_run_id is None
    assert rep.cost_usd is None
    assert rep.items_scraped == 0


def test_fetch_reports_timeout_run_with_partial_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout = run abortado que cobró lo consumido: el report lleva ese costo parcial."""
    exc = ApifyTimeoutError(
        "run 'RT' timed out", run_id="RT", usage_usd=0.02, charged_events={"result": 5}
    )
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_raising(exc))
    src = InstagramSource(_cfg())
    assert list(src.fetch(SocialCursor())) == []
    [rep] = src.pop_run_reports()
    assert rep.status == "timeout"
    assert rep.apify_run_id == "RT"
    assert rep.cost_usd == 0.02
    assert rep.charged_events == {"result": 5}


# ---- advance_checkpoint ---- #


def _rec(external_id: str, dt: datetime) -> SourceRecord:
    return SourceRecord(external_id=external_id, occurred_at=dt, payload={}, dedupe_keys=[])


def test_advance_checkpoint_sets_account_entry() -> None:
    src = InstagramSource(_cfg())
    dt = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    cp = src.advance_checkpoint(SocialCursor(), _rec("instagram:utn.frba:p9", dt))
    assert cp.accounts["utn.frba"].last_post_id == "p9"
    assert cp.accounts["utn.frba"].last_posted_at == dt


def test_advance_checkpoint_preserves_other_accounts() -> None:
    src = InstagramSource(_cfg())
    existing = SocialCursor(accounts={"fiuba": AccountCursor(last_post_id="z")})
    dt = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    cp = src.advance_checkpoint(existing, _rec("instagram:utn.frba:p1", dt))
    assert cp.accounts["fiuba"].last_post_id == "z"
    assert cp.accounts["utn.frba"].last_post_id == "p1"


def test_advance_checkpoint_ignores_foreign_external_id() -> None:
    src = InstagramSource(_cfg())
    existing = SocialCursor(accounts={"utn.frba": AccountCursor(last_post_id="p1")})
    bad = _rec("telegram:-100:42", datetime(2026, 5, 28, tzinfo=UTC))
    assert src.advance_checkpoint(existing, bad) is existing


# ---- helpers ---- #


def test_split_social_external_id() -> None:
    assert split_social_external_id("x:utnfrba:123") == ("x", "utnfrba", "123")
    # post_id may contain colons (maxsplit=2)
    assert split_social_external_id("facebook:utn:a:b") == ("facebook", "utn", "a:b")
    assert split_social_external_id("imap:server:1") is None  # bad prefix
    assert split_social_external_id("instagram:acct") is None  # too few parts
    assert split_social_external_id("instagram::p1") is None  # empty account


def test_is_new_record() -> None:
    dt = datetime(2026, 5, 28, 11, 0, tzinfo=UTC)
    rec = _rec("instagram:utn.frba:p2", dt)
    assert is_new_record(rec, None) is True
    assert is_new_record(rec, AccountCursor()) is True  # last_posted_at None
    older = AccountCursor(last_post_id="p1", last_posted_at=datetime(2026, 5, 28, 10, tzinfo=UTC))
    assert is_new_record(rec, older) is True
    same_id = AccountCursor(last_post_id="p2", last_posted_at=dt)
    assert is_new_record(rec, same_id) is False  # exact boundary post
    same_ts_diff_id = AccountCursor(last_post_id="pX", last_posted_at=dt)
    assert is_new_record(rec, same_ts_diff_id) is True  # same second, different post


def test_advance_social_checkpoint_is_pure() -> None:
    dt = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    cp = advance_social_checkpoint(SocialCursor(), _rec("x:utnfrba:9", dt))
    assert cp.accounts["utnfrba"] == AccountCursor(last_post_id="9", last_posted_at=dt)


# ---- health_check ---- #


@pytest.mark.asyncio
async def test_health_check_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OK:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> _OK:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def whoami(self) -> dict[str, Any]:
            return {"username": "tester"}

    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _OK)
    result = await InstagramSource(_cfg()).health_check()
    assert isinstance(result, HealthResult)
    assert result.status == "healthy"
    assert "tester" in result.detail


@pytest.mark.asyncio
async def test_health_check_unhealthy_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Bad:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> _Bad:
            raise ConnectionError("apify unreachable")

        async def __aexit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _Bad)
    result = await InstagramSource(_cfg()).health_check()
    assert result.status == "unhealthy"
    assert "ConnectionError" in result.detail


# ---- media: download bytes (extract_media) ---- #


@respx.mock
def test_fetch_does_not_download_when_extract_media_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin `extract_media`: la metadata (media_refs) va en el payload, pero NO se bajan bytes."""
    items = [_ig_media("2026-05-28T10:00:00Z", "p1", img="https://cdn.example/a.jpg")]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    recs = list(InstagramSource(_cfg()).fetch(SocialCursor()))
    assert recs[0].media == []
    assert recs[0].payload["media_refs"][0]["url"] == "https://cdn.example/a.jpg"
    assert not respx.calls  # nunca pegó al CDN


@respx.mock
def test_fetch_downloads_image_media(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_ig_media("2026-05-28T10:00:00Z", "p1", img="https://cdn.example/a.jpg")]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    body = b"\xff\xd8\xff-image-bytes"
    respx.get("https://cdn.example/a.jpg").mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )
    recs = list(InstagramSource(_cfg(extract_media=True)).fetch(SocialCursor()))
    assert len(recs) == 1
    media = recs[0].media
    assert len(media) == 1
    assert media[0].content_type == "image/jpeg"
    assert media[0].size == len(body)
    assert len(media[0].sha256) == 64
    assert media[0].filename == "a.jpg"


@respx.mock
def test_fetch_downloads_video_media(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_ig_media("2026-05-28T10:00:00Z", "p1", video="https://cdn.example/v.mp4")]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    respx.get("https://cdn.example/v.mp4").mock(
        return_value=httpx.Response(
            200, content=b"\x00\x00video", headers={"content-type": "video/mp4"}
        )
    )
    recs = list(InstagramSource(_cfg(extract_media=True)).fetch(SocialCursor()))
    media = recs[0].media
    assert len(media) == 1
    assert media[0].content_type == "video/mp4"


@respx.mock
def test_fetch_skips_too_large_media(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_ig_media("2026-05-28T10:00:00Z", "p1", img="https://cdn.example/big.jpg")]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    respx.get("https://cdn.example/big.jpg").mock(
        return_value=httpx.Response(200, content=b"x" * 100, headers={"content-type": "image/jpeg"})
    )
    cfg = _cfg(extract_media=True, max_attachment_bytes=10)
    recs = list(InstagramSource(cfg).fetch(SocialCursor()))
    assert recs[0].media == []  # superó el tope → salteado, no tumba el post


@respx.mock
def test_fetch_skips_media_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_ig_media("2026-05-28T10:00:00Z", "p1", img="https://cdn.example/gone.jpg")]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    respx.get("https://cdn.example/gone.jpg").mock(return_value=httpx.Response(404))
    recs = list(InstagramSource(_cfg(extract_media=True)).fetch(SocialCursor()))
    assert recs[0].media == []


@respx.mock
def test_fetch_skips_non_whitelisted_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un CDN que devuelve 200 con HTML (ej. página de error) NO se guarda como media."""
    items = [_ig_media("2026-05-28T10:00:00Z", "p1", img="https://cdn.example/err.jpg")]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    respx.get("https://cdn.example/err.jpg").mock(
        return_value=httpx.Response(
            200, content=b"<html>nope</html>", headers={"content-type": "text/html"}
        )
    )
    recs = list(InstagramSource(_cfg(extract_media=True)).fetch(SocialCursor()))
    assert recs[0].media == []


# ---- cursor: per-account advance through the runner ---- #


def test_runner_fold_advances_every_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Con varias cuentas en un mismo chunk, el fold del runner avanza el cursor de TODAS,
    no solo la última (el fix de avance por-cuenta)."""
    items = [_ig("2026-05-28T10:00:00Z", "p1")]
    monkeypatch.setattr("memex.ingestors.social._common.ApifyClient", _fake_apify_returning(items))
    src = InstagramSource(_cfg(accounts=[AllowedAccount(account="a"), AllowedAccount(account="b")]))
    client = MagicMock()
    client.get_checkpoint.return_value = None
    client.post_ingest_batch.return_value = {
        "inserted": 2,
        "duplicates": 0,
        "errors": 0,
        "filtered": 0,
    }

    run_ingestor(src, source_id=7, sink=client, chunk_size=10, chunk_sleep_ms=0)

    client.put_checkpoint.assert_called_once()
    saved = client.put_checkpoint.call_args[0][1]
    assert set(saved["accounts"].keys()) == {"a", "b"}
    assert saved["accounts"]["a"]["last_post_id"] == "p1"
    assert saved["accounts"]["b"]["last_post_id"] == "p1"
