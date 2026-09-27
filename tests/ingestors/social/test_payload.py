"""SocialPostPayload + SocialEngagement — shape, immutability, defaults, discriminator."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from memex.core.payloads import SocialEngagement, SocialPostPayload


def _base_kwargs() -> dict[str, Any]:
    return {
        "platform": "instagram",
        "account": "utn.frba",
        "post_id": "abc123",
        "url": "https://www.instagram.com/p/abc123/",
        "posted_at": datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
    }


def test_minimal_payload_has_defaults() -> None:
    p = SocialPostPayload(**_base_kwargs())
    assert p.platform == "instagram"
    assert p.account == "utn.frba"
    assert p.post_id == "abc123"
    assert p.text == ""
    assert p.shortcode is None
    assert p.account_name is None
    assert p.media_kind == "none"
    assert p.engagement is None
    assert p.is_paid_partnership is None
    assert p.raw_type is None


def test_full_payload_roundtrips_via_json() -> None:
    p = SocialPostPayload(
        platform="x",
        account="utnfrba",
        account_name="UTN FRBA",
        post_id="1700000000000000000",
        shortcode=None,
        url="https://x.com/utnfrba/status/1700000000000000000",
        text="hackathon abierto",
        posted_at=datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
        media_kind="image",
        engagement=SocialEngagement(likes=10, comments=2, shares=3, views=100),
        is_paid_partnership=None,
        raw_type="tweet",
    )
    blob = p.model_dump(mode="json", by_alias=True)
    p2 = SocialPostPayload.model_validate(blob)
    assert p2 == p


def test_payload_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SocialPostPayload.model_validate({**_base_kwargs(), "bogus": "x"})


def test_payload_is_frozen() -> None:
    p = SocialPostPayload(**_base_kwargs())
    with pytest.raises(ValidationError):
        p.text = "mutate"  # type: ignore[misc]


def test_payload_rejects_unknown_platform() -> None:
    with pytest.raises(ValidationError):
        SocialPostPayload(**{**_base_kwargs(), "platform": "tiktok"})


def test_payload_rejects_unknown_media_kind() -> None:
    with pytest.raises(ValidationError):
        SocialPostPayload(**{**_base_kwargs(), "media_kind": "hologram"})


def test_engagement_defaults_all_none() -> None:
    e = SocialEngagement()
    assert e.likes is None
    assert e.comments is None
    assert e.shares is None
    assert e.views is None


def test_engagement_is_frozen_and_forbids_extra() -> None:
    e = SocialEngagement(likes=1)
    with pytest.raises(ValidationError):
        e.likes = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SocialEngagement.model_validate({"likes": 1, "bogus": True})
