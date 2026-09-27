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

from memex.core.payloads import SocialEngagement, SocialMediaRef, SocialPostPayload
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
        media_refs=_ig_media_refs(item),
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
    media_refs = _fb_media_refs(item)
    return _build_record(
        platform="facebook",
        account=account,
        post_id=post_id,
        shortcode=None,
        url=url,
        text=_as_str(_first(item, "text", "message", "postText")) or "",
        posted_at=posted_at,
        media_kind=_fb_media_kind(item, media_refs),
        media_refs=media_refs,
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
        media_refs=_x_media_refs(item),
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
    media_refs: list[SocialMediaRef],
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
        media_refs=media_refs,
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


def _fb_media_kind(item: dict[str, Any], refs: list[SocialMediaRef]) -> MediaKind:
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
    # Fallback: derivar el kind de los refs ya colectados. Cubre la imagen anidada en
    # `media[].photo_image`, que el heurístico top-level no ve, evitando media_kind="none"
    # con media_refs no vacío. El video gana sobre la imagen.
    if refs:
        return "video" if any(r.kind == "video" for r in refs) else "image"
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


class _MediaRefCollector:
    """Acumula `SocialMediaRef` deduplicando por URL, preservando el orden de inserción."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.refs: list[SocialMediaRef] = []

    def add(
        self, url: Any, kind: Literal["image", "video"], content_type: str | None = None
    ) -> None:
        u = _as_str(url)
        if not u or u in self._seen:
            return
        self._seen.add(u)
        self.refs.append(SocialMediaRef(url=u, kind=kind, content_type=content_type))


def _ig_media_refs(item: dict[str, Any]) -> list[SocialMediaRef]:
    """URLs de media de un post de Instagram (foto / video / carrusel).

    En videos/reels, `displayUrl` es el poster (OCR-able) y `videoUrl` el video. Los
    carruseles (`childPosts`) llevan una entrada por slide.
    """
    c = _MediaRefCollector()
    c.add(_first(item, "displayUrl", "display_url"), "image")
    _add_image_list(c, _first(item, "images"))
    c.add(_first(item, "videoUrl", "video_url"), "video")
    children = _first(item, "childPosts", "child_posts", "sidecarChildren")
    if isinstance(children, list):
        for ch in children:
            if not isinstance(ch, dict):
                continue
            c.add(_first(ch, "displayUrl", "display_url"), "image")
            c.add(_first(ch, "videoUrl", "video_url"), "video")
    return c.refs


def _fb_media_refs(item: dict[str, Any]) -> list[SocialMediaRef]:
    """URLs de media de un post de Facebook. El shape de `media[]` varía bastante; defensivo."""
    c = _MediaRefCollector()
    c.add(_first(item, "imageUrl", "image"), "image")
    c.add(_first(item, "thumbnailUrl", "thumbnail"), "image")
    _add_image_list(c, _first(item, "images"))
    c.add(_first(item, "videoUrl", "video_url"), "video")
    media = _first(item, "media", "attachments")
    if isinstance(media, list):
        for m in media:
            if not isinstance(m, dict):
                continue
            photo = m.get("photo_image")
            if isinstance(photo, dict):
                c.add(_first(photo, "uri", "url"), "image")
            c.add(_first(m, "thumbnail", "thumbnailUrl", "image", "url", "src"), "image")
            c.add(_first(m, "videoUrl", "video_url"), "video")
    return c.refs


def _x_media_refs(item: dict[str, Any]) -> list[SocialMediaRef]:
    """URLs de media de un tweet.

    `apidojo/tweet-scraper` expone la media en dos formas que combinamos (dedup por URL):
    - `media`: lista CONVENIENTE de URLs (strings) — el poster/imagen ya resuelta.
    - `media` (dicts) + `extendedEntities`/`extended_entities`/`entities`.media: objetos
      ricos con `type` y, para video, `video_info.variants` (elegimos la mejor mp4) + poster.
    """
    c = _MediaRefCollector()
    media = item.get("media")
    if isinstance(media, list):
        for m in media:
            if isinstance(m, str):
                c.add(m, "image")
    for m in _x_media_dicts(item):
        if _x_is_video(m):
            url, ctype = _best_x_video_variant(m.get("video_info") or m.get("videoInfo"))
            c.add(url, "video", ctype)
            c.add(_first(m, "media_url_https", "media_url", "thumbnail"), "image")  # poster
        else:
            c.add(_first(m, "media_url_https", "media_url", "url"), "image")
    return c.refs


def _add_image_list(c: _MediaRefCollector, value: Any) -> None:
    """Agrega una lista de imágenes que puede venir como list[str] o list[dict]."""
    if not isinstance(value, list):
        return
    for im in value:
        if isinstance(im, str):
            c.add(im, "image")
        elif isinstance(im, dict):
            c.add(_first(im, "url", "src", "displayUrl", "uri"), "image")


def _x_media_dicts(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Objetos de media (dicts) de un tweet: de `media` y de
    `extendedEntities`/`extended_entities`/`entities` (.media). El caller deduplica por URL."""
    out: list[dict[str, Any]] = []
    media = item.get("media")
    if isinstance(media, list):
        out.extend(m for m in media if isinstance(m, dict))
    for key in ("extendedEntities", "extended_entities", "entities"):
        ent = item.get(key)
        nested = ent.get("media") if isinstance(ent, dict) else ent
        if isinstance(nested, list):
            out.extend(m for m in nested if isinstance(m, dict))
    return out


def _x_is_video(m: dict[str, Any]) -> bool:
    """True si el media de X es video/gif (por `type`, `video_info` o `/video/` en la URL)."""
    if str(m.get("type", "")).lower() in ("video", "animated_gif"):
        return True
    if "video_info" in m or "videoInfo" in m:
        return True
    return "/video/" in str(m.get("expanded_url") or m.get("expandedUrl") or "")


def _best_x_video_variant(video_info: Any) -> tuple[str | None, str | None]:
    """Elige la variante de mayor bitrate de un video de X (URL, content_type).

    Saltea playlists `m3u8` (no son un archivo de bytes descargable directo). Devuelve
    `(None, None)` si no hay variante mp4 usable.
    """
    if not isinstance(video_info, dict):
        return None, None
    variants = video_info.get("variants")
    if not isinstance(variants, list):
        return None, None
    best_url: str | None = None
    best_ct: str | None = None
    best_bitrate = -1
    for v in variants:
        if not isinstance(v, dict):
            continue
        url = _as_str(v.get("url"))
        if not url:
            continue
        ctype = _as_str(v.get("content_type") or v.get("contentType"))
        if ctype and "mpegurl" in ctype.lower():
            continue  # m3u8 playlist
        bitrate = v.get("bitrate")
        b = bitrate if isinstance(bitrate, int) and not isinstance(bitrate, bool) else 0
        if b > best_bitrate:
            best_bitrate = b
            best_url = url
            best_ct = ctype
    return best_url, best_ct


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
