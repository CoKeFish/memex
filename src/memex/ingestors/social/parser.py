"""Parsers: convierten un item de dataset de Apify en un `SourceRecord` con `SocialPostPayload`.

Un parser por plataforma (`parse_instagram_item`, `parse_facebook_item`, `parse_x_item`).
Todos:

- reciben `account` = el identificador de la **allowlist** (no el owner scrapeado), y lo
  usan tal cual en el `external_id` y el payload. Esto es crítico: `fetch` busca el cursor
  por la cuenta de la allowlist y `advance_checkpoint` lo recupera del `external_id`; si
  usáramos el owner scrapeado (ej. el `pageId` numérico de Facebook), la key no matchearía
  y el checkpoint NUNCA persistiría → re-fetch infinito desde cero.
- son **defensivos**: los shapes de los actores de Apify varían y no están garantizados;
  probamos varias claves alternativas y devolvemos `None` si falta lo esencial (`post_id` o
  `posted_at`). El caller filtra los `None`.
- devuelven `occurred_at` tz-aware en UTC.

`external_id`: `{platform}:{account}:{post_id}`.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from memex.core.payloads import SocialEngagement, SocialPostPayload
from memex.core.source import SourceRecord

MediaKind = Literal["none", "image", "video", "carousel", "reel", "other"]

_IG_MEDIA: dict[str, MediaKind] = {
    "image": "image",
    "photo": "image",
    "sidecar": "carousel",
    "carousel": "carousel",
    "video": "video",
    "reel": "reel",
}


def parse_instagram_item(item: dict[str, Any], account: str) -> SourceRecord | None:
    """Parsea un post del actor `apify/instagram-scraper`."""
    post_id = _as_str(_first(item, "id", "shortCode", "shortcode"))
    if post_id is None:
        return None
    posted_at = _parse_dt(_first(item, "timestamp", "takenAtTimestamp", "taken_at"))
    if posted_at is None:
        return None

    shortcode = _as_str(_first(item, "shortCode", "shortcode"))
    url = _as_str(_first(item, "url")) or (
        f"https://www.instagram.com/p/{shortcode}/"
        if shortcode
        else f"https://www.instagram.com/{account}/"
    )
    raw_type = _as_str(_first(item, "type", "productType"))
    is_sponsored = _first(item, "isSponsored", "is_paid_partnership")
    return _build_record(
        platform="instagram",
        account=account,
        post_id=post_id,
        shortcode=shortcode,
        url=url,
        text=_as_str(_first(item, "caption")) or "",
        posted_at=posted_at,
        media_kind=_lookup_media(raw_type, _IG_MEDIA),
        engagement=_engagement(
            likes=_as_int(_first(item, "likesCount", "likes")),
            comments=_as_int(_first(item, "commentsCount", "comments")),
            views=_as_int(_first(item, "videoViewCount", "videoPlayCount", "views")),
        ),
        account_name=_as_str(_first(item, "ownerFullName", "ownerUsername")),
        is_paid_partnership=is_sponsored if isinstance(is_sponsored, bool) else None,
        raw_type=raw_type,
    )


def parse_facebook_item(item: dict[str, Any], account: str) -> SourceRecord | None:
    """Parsea un post del actor `apify/facebook-posts-scraper`."""
    post_id = _as_str(_first(item, "postId", "post_id", "id"))
    if post_id is None:
        return None
    posted_at = _parse_dt(_first(item, "time", "timestamp", "date", "publishedTime"))
    if posted_at is None:
        return None

    url = _as_str(_first(item, "url", "postUrl", "link")) or f"https://www.facebook.com/{account}"
    return _build_record(
        platform="facebook",
        account=account,
        post_id=post_id,
        shortcode=None,
        url=url,
        text=_as_str(_first(item, "text", "message", "postText")) or "",
        posted_at=posted_at,
        media_kind=_fb_media_kind(item),
        engagement=_engagement(
            likes=_as_int(_first(item, "likes", "likesCount", "reactionsCount", "reactions")),
            comments=_as_int(_first(item, "comments", "commentsCount")),
            shares=_as_int(_first(item, "shares", "sharesCount")),
        ),
        account_name=_as_str(_first(item, "pageName", "user", "authorName")),
        is_paid_partnership=None,
        raw_type=_as_str(_first(item, "type", "mediaType")),
    )


def parse_x_item(item: dict[str, Any], account: str) -> SourceRecord | None:
    """Parsea un tweet del actor `apidojo/tweet-scraper`."""
    post_id = _as_str(_first(item, "id", "id_str", "tweetId"))
    if post_id is None:
        return None
    posted_at = _parse_dt(_first(item, "createdAt", "created_at", "timestamp", "date"))
    if posted_at is None:
        return None

    url = (
        _as_str(_first(item, "url", "twitterUrl", "tweetUrl"))
        or f"https://x.com/{account}/status/{post_id}"
    )
    author = _first(item, "author", "user")
    account_name = None
    if isinstance(author, dict):
        account_name = _as_str(_first(author, "name", "userName", "screen_name"))
    return _build_record(
        platform="x",
        account=account,
        post_id=post_id,
        shortcode=None,
        url=url,
        text=_as_str(_first(item, "text", "full_text", "fullText")) or "",
        posted_at=posted_at,
        media_kind=_x_media_kind(item),
        engagement=_engagement(
            likes=_as_int(_first(item, "likeCount", "favoriteCount", "favorite_count", "likes")),
            comments=_as_int(_first(item, "replyCount", "reply_count", "replies")),
            shares=_as_int(_first(item, "retweetCount", "retweet_count", "retweets")),
            views=_as_int(_first(item, "viewCount", "views")),
        ),
        account_name=account_name,
        is_paid_partnership=None,
        raw_type=_as_str(_first(item, "type")),
    )


def _build_record(
    *,
    platform: Literal["instagram", "facebook", "x"],
    account: str,
    post_id: str,
    shortcode: str | None,
    url: str,
    text: str,
    posted_at: datetime,
    media_kind: MediaKind,
    engagement: SocialEngagement | None,
    account_name: str | None,
    is_paid_partnership: bool | None,
    raw_type: str | None,
) -> SourceRecord:
    payload = SocialPostPayload(
        platform=platform,
        account=account,
        account_name=account_name,
        post_id=post_id,
        shortcode=shortcode,
        url=url,
        text=text,
        posted_at=posted_at,
        media_kind=media_kind,
        engagement=engagement,
        is_paid_partnership=is_paid_partnership,
        raw_type=raw_type,
    )
    external_id = f"{platform}:{account}:{post_id}"
    return SourceRecord(
        external_id=external_id,
        occurred_at=posted_at,
        payload=payload.model_dump(mode="json", by_alias=True),
        dedupe_keys=[external_id],
    )


def _fb_media_kind(item: dict[str, Any]) -> MediaKind:
    t = _as_str(_first(item, "type", "mediaType"))
    if t:
        tl = t.lower()
        if "video" in tl:
            return "video"
        if "photo" in tl or "image" in tl:
            return "image"
    if _first(item, "videoUrl", "video") is not None:
        return "video"
    if _first(item, "imageUrl", "images", "photo") is not None:
        return "image"
    return "none"


def _x_media_kind(item: dict[str, Any]) -> MediaKind:
    media = _first(item, "media", "extendedEntities", "extended_entities")
    if isinstance(media, list) and media:
        kinds = {str(m.get("type", "")).lower() for m in media if isinstance(m, dict)}
        if "video" in kinds or "animated_gif" in kinds:
            return "video"
        if "photo" in kinds or "image" in kinds:
            return "image"
        return "other"
    return "none"


def _lookup_media(raw_type: str | None, table: dict[str, MediaKind]) -> MediaKind:
    if not raw_type:
        return "none"
    return table.get(raw_type.lower(), "other")


def _engagement(
    *,
    likes: int | None = None,
    comments: int | None = None,
    shares: int | None = None,
    views: int | None = None,
) -> SocialEngagement | None:
    if likes is None and comments is None and shares is None and views is None:
        return None
    return SocialEngagement(likes=likes, comments=comments, shares=shares, views=views)


def _first(item: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        value = item.get(k)
        if value is not None:
            return value
    return None


def _as_str(value: Any) -> str | None:
    """Coerce a scalar to str. dict/list → None (no stringified `{...}` garbage)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # JSON `1e999` decodes to inf; NaN/inf would crash int() — drop them.
        if not math.isfinite(value):
            return None
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _parse_dt(value: Any) -> datetime | None:
    """Parsea un timestamp a datetime tz-aware UTC. None si no se reconoce.

    Maneja: datetime nativo, epoch (int/float segundos), ISO 8601 (con `Z`), y el
    formato clásico de Twitter (`"Wed Oct 25 12:34:56 +0000 2023"`).
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(value, str):
        return None

    s = value.strip()
    if not s:
        return None

    iso = f"{s[:-1]}+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        pass

    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None
