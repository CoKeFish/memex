"""Parsers IG/FB/X — external_id shape, tz-aware dates, defensive rejection.

Usa dicts inline con la shape documentada de cada actor de Apify (las claves reales
no están garantizadas y varían — por eso los parsers son defensivos). No instancia
tipos de Apify reales: los actores devuelven JSON plano.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memex.ingestors.social.parser import (
    parse_facebook_item,
    parse_instagram_item,
    parse_x_item,
)

# ---- Instagram ---- #


def _ig_item(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "3001",
        "shortCode": "ABC123",
        "caption": "Convocatoria abierta",
        "timestamp": "2026-05-28T10:00:00.000Z",
        "url": "https://www.instagram.com/p/ABC123/",
        "likesCount": 10,
        "commentsCount": 2,
        "videoViewCount": 0,
        "type": "Image",
        "ownerUsername": "scraped_owner",
        "ownerFullName": "UTN FRBA",
    }
    base.update(overrides)
    return base


def test_instagram_parses_full_post() -> None:
    rec = parse_instagram_item(_ig_item(), "utn.frba")
    assert rec is not None
    assert rec.external_id == "instagram:utn.frba:3001"
    assert rec.dedupe_keys == ["instagram:utn.frba:3001"]
    assert rec.occurred_at == datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    assert rec.payload["platform"] == "instagram"
    assert rec.payload["shortcode"] == "ABC123"
    assert rec.payload["text"] == "Convocatoria abierta"
    assert rec.payload["media_kind"] == "image"
    assert rec.payload["engagement"]["likes"] == 10


def test_instagram_carousel_and_reel_media_kinds() -> None:
    assert parse_instagram_item(_ig_item(type="Sidecar"), "x").payload["media_kind"] == "carousel"  # type: ignore[union-attr]
    assert parse_instagram_item(_ig_item(type="Video"), "x").payload["media_kind"] == "video"  # type: ignore[union-attr]


def test_instagram_rejects_missing_id() -> None:
    item = _ig_item()
    del item["id"]
    del item["shortCode"]
    assert parse_instagram_item(item, "utn.frba") is None


def test_instagram_rejects_missing_timestamp() -> None:
    item = _ig_item()
    del item["timestamp"]
    assert parse_instagram_item(item, "utn.frba") is None


# ---- Facebook ---- #


def _fb_item(**overrides: Any) -> dict[str, Any]:
    base = {
        "postId": "pfbid0XYZ",
        "text": "Hackathon este finde",
        "time": "2026-05-28T09:00:00Z",
        "url": "https://www.facebook.com/utn/posts/pfbid0XYZ",
        "likes": 5,
        "comments": 1,
        "shares": 3,
        "pageName": "UTN",
        "pageId": "999888777",
        "type": "Video",
    }
    base.update(overrides)
    return base


def test_facebook_parses_full_post() -> None:
    rec = parse_facebook_item(_fb_item(), "utn")
    assert rec is not None
    assert rec.external_id == "facebook:utn:pfbid0XYZ"
    assert rec.occurred_at == datetime(2026, 5, 28, 9, 0, tzinfo=UTC)
    assert rec.payload["text"] == "Hackathon este finde"
    assert rec.payload["media_kind"] == "video"
    assert rec.payload["engagement"]["shares"] == 3


def test_facebook_rejects_missing_id_and_time() -> None:
    item = _fb_item()
    del item["postId"]
    assert parse_facebook_item(item, "utn") is None
    item2 = _fb_item()
    del item2["time"]
    assert parse_facebook_item(item2, "utn") is None


# ---- X (Twitter) ---- #


def _x_item(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "1700000000000000000",
        "text": "Abrimos inscripciones al hackathon",
        "createdAt": "Wed Oct 25 12:34:56 +0000 2023",
        "url": "https://x.com/utnfrba/status/1700000000000000000",
        "likeCount": 7,
        "retweetCount": 2,
        "replyCount": 1,
        "viewCount": 100,
        "author": {"userName": "scraped_handle", "name": "UTN FRBA"},
        "media": [{"type": "photo"}],
    }
    base.update(overrides)
    return base


def test_x_parses_full_tweet() -> None:
    rec = parse_x_item(_x_item(), "utnfrba")
    assert rec is not None
    assert rec.external_id == "x:utnfrba:1700000000000000000"
    assert rec.occurred_at == datetime(2023, 10, 25, 12, 34, 56, tzinfo=UTC)
    assert rec.payload["text"].startswith("Abrimos")
    assert rec.payload["media_kind"] == "image"
    assert rec.payload["engagement"]["shares"] == 2  # retweets -> shares
    assert rec.payload["engagement"]["comments"] == 1  # replies -> comments
    assert rec.payload["engagement"]["views"] == 100


def test_x_accepts_iso_and_epoch_dates() -> None:
    iso = parse_x_item(_x_item(createdAt="2026-01-02T03:04:05Z"), "h")
    assert iso is not None
    assert iso.occurred_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    epoch = parse_x_item(_x_item(createdAt=1735787045), "h")
    assert epoch is not None
    assert epoch.occurred_at.tzinfo is not None


def test_x_rejects_unparseable_date() -> None:
    assert parse_x_item(_x_item(createdAt="not a date"), "h") is None


def test_non_finite_engagement_does_not_crash() -> None:
    """JSON `1e999` decodifica a inf; int(inf)/int(nan) crashearían. El parser debe
    dropear esos valores a None en vez de propagar OverflowError/ValueError."""
    rec = parse_x_item(_x_item(likeCount=float("inf"), retweetCount=float("nan")), "h")
    assert rec is not None
    eng = rec.payload["engagement"]
    assert eng["likes"] is None  # inf -> None
    assert eng["shares"] is None  # nan -> None
    assert eng["views"] == 100  # finito, intacto


def test_as_str_returns_none_for_nested_object() -> None:
    """account_name de un campo que es un objeto anidado (no scalar) → None, no un
    dict stringificado. (apify/facebook-posts-scraper a veces trae `user` como objeto.)"""
    item = _fb_item()
    del item["pageName"]
    item["user"] = {"name": "UTN", "id": 1}
    rec = parse_facebook_item(item, "utn")
    assert rec is not None
    assert rec.payload["account_name"] is None


# ---- Regression: external_id account segment comes from the ALLOWLIST ---- #


def test_account_segment_uses_allowlist_not_scraped_owner() -> None:
    """El segmento `account` del external_id sale del parámetro (allowlist), NUNCA
    del owner scrapeado. Si usara el owner, la key del cursor no matchearía la de
    la allowlist y el checkpoint nunca persistiría → re-fetch infinito.

    Los fixtures traen owners distintos a propósito (`scraped_owner` / `pageId` /
    `scraped_handle`)."""
    ig = parse_instagram_item(_ig_item(), "utn.frba")
    fb = parse_facebook_item(_fb_item(), "utn")
    x = parse_x_item(_x_item(), "utnfrba")
    assert ig is not None and fb is not None and x is not None

    assert ig.external_id.split(":")[1] == "utn.frba"
    assert ig.payload["account"] == "utn.frba"
    # Facebook: NUNCA el pageId numérico scrapeado.
    assert fb.external_id.split(":")[1] == "utn"
    assert "999888777" not in fb.external_id
    # X: NUNCA el handle scrapeado del author.
    assert x.external_id.split(":")[1] == "utnfrba"
    assert "scraped_handle" not in x.external_id


# ---- media_refs: extracción de URLs de media ---- #


def test_instagram_media_refs_image_and_video() -> None:
    rec = parse_instagram_item(
        _ig_item(displayUrl="https://cdn/ig.jpg", type="Video", videoUrl="https://cdn/ig.mp4"),
        "utn.frba",
    )
    assert rec is not None
    refs = {(r["url"], r["kind"]) for r in rec.payload["media_refs"]}
    assert ("https://cdn/ig.jpg", "image") in refs
    assert ("https://cdn/ig.mp4", "video") in refs


def test_instagram_carousel_media_refs() -> None:
    item = _ig_item(
        type="Sidecar",
        childPosts=[
            {"displayUrl": "https://cdn/1.jpg"},
            {"displayUrl": "https://cdn/2.jpg", "videoUrl": "https://cdn/2.mp4"},
        ],
    )
    rec = parse_instagram_item(item, "x")
    assert rec is not None
    urls = [r["url"] for r in rec.payload["media_refs"]]
    assert "https://cdn/1.jpg" in urls
    assert "https://cdn/2.jpg" in urls
    assert "https://cdn/2.mp4" in urls


def test_instagram_media_refs_empty_when_no_media() -> None:
    item = _ig_item()
    item.pop("displayUrl", None)
    rec = parse_instagram_item(item, "utn.frba")
    assert rec is not None
    assert rec.payload["media_refs"] == []


def test_facebook_media_refs() -> None:
    item = _fb_item(
        media=[{"photo_image": {"uri": "https://cdn/fb.jpg"}}],
        videoUrl="https://cdn/fb.mp4",
    )
    rec = parse_facebook_item(item, "utn")
    assert rec is not None
    refs = {(r["url"], r["kind"]) for r in rec.payload["media_refs"]}
    assert ("https://cdn/fb.jpg", "image") in refs
    assert ("https://cdn/fb.mp4", "video") in refs


def test_x_media_refs_photo_and_best_video_variant() -> None:
    item = _x_item(
        media=[
            {"type": "photo", "media_url_https": "https://cdn/x.jpg"},
            {
                "type": "video",
                "media_url_https": "https://cdn/poster.jpg",
                "video_info": {
                    "variants": [
                        {
                            "bitrate": 256000,
                            "content_type": "video/mp4",
                            "url": "https://cdn/lo.mp4",
                        },
                        {
                            "bitrate": 832000,
                            "content_type": "video/mp4",
                            "url": "https://cdn/hi.mp4",
                        },
                        {"content_type": "application/x-mpegURL", "url": "https://cdn/play.m3u8"},
                    ]
                },
            },
        ],
    )
    rec = parse_x_item(item, "utnfrba")
    assert rec is not None
    by_url = {r["url"]: r for r in rec.payload["media_refs"]}
    assert "https://cdn/x.jpg" in by_url
    assert "https://cdn/poster.jpg" in by_url  # poster del video como imagen (OCR-able)
    assert by_url["https://cdn/hi.mp4"]["kind"] == "video"  # mayor bitrate
    assert by_url["https://cdn/hi.mp4"]["content_type"] == "video/mp4"
    assert "https://cdn/lo.mp4" not in by_url  # bitrate menor descartado
    assert "https://cdn/play.m3u8" not in by_url  # playlist m3u8 descartada


def test_x_media_refs_apidojo_shape() -> None:
    """Shape real de `apidojo/tweet-scraper`: `media` = lista de URLs (strings) + el objeto
    rico en `extendedEntities.media` (video sin `video_info`). Antes daba 0 refs porque `media`
    (no vacío) tapaba a `extendedEntities`. Esperamos 1 ref imagen (el poster), deduplicado."""
    poster = "https://pbs.twimg.com/media/HJwKD0pWEAA5e1u.png"
    item = _x_item(
        media=[poster],
        extendedEntities={
            "media": [
                {
                    "media_url_https": poster,
                    "expanded_url": "https://x.com/NASA/status/2061/video/1",
                }
            ]
        },
        entities={"media": [{"media_url_https": poster}]},
    )
    rec = parse_x_item(item, "nasa")
    assert rec is not None
    assert rec.payload["media_refs"] == [{"url": poster, "kind": "image", "content_type": None}]
