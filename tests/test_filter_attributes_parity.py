"""Paridad entre los payload models y la referencia de atributos filtrables del dashboard.

La vista /filtros documenta in-page qué atributos del payload acepta el scope de `filter_rules`
(`frontend/src/lib/filter-attributes.ts`). Esa referencia es TS estático curado; este test deriva
los dot-paths REALES desde los modelos Pydantic de `memex.core.payloads` y los compara contra los
VECTORES ESPEJO (duplicados acá y en `filter-attributes.test.ts`, convención de
`render-payload.ts` ↔ `tests/test_processing_render.py`). Cambiar un campo en `payloads.py` rompe
este test y obliga a actualizar la referencia de ambos lados. Sin DB.
"""

from __future__ import annotations

import types
from typing import Union, get_args, get_origin

from pydantic import BaseModel

from memex.core.payloads import EmailPayload, SocialPostPayload, TelegramPayload

# --- VECTORES ESPEJO de frontend/src/lib/filter-attributes.test.ts --------------- #

EXPECTED_EMAIL_PATHS = [
    "attachments",
    "auto_submitted",
    "body_source",
    "body_text",
    "body_truncated",
    "cc",
    "date",
    "flags",
    "folder",
    "from.email",
    "from.name",
    "in_reply_to",
    "list_id",
    "list_unsubscribe",
    "list_unsubscribe_post",
    "message_id",
    "precedence",
    "raw_headers",
    "references",
    "reply_to",
    "size_bytes",
    "subject",
    "to",
]

EXPECTED_TELEGRAM_PATHS = [
    "chat_id",
    "chat_kind",
    "chat_title",
    "date",
    "forwarded_from",
    "media_caption",
    "media_kind",
    "message_id",
    "reply_to_message_id",
    "sender.display_name",
    "sender.is_bot",
    "sender.user_id",
    "sender.username",
    "text",
    "topic_id",
]

EXPECTED_SOCIAL_PATHS = [
    "account",
    "account_name",
    "engagement.comments",
    "engagement.likes",
    "engagement.shares",
    "engagement.views",
    "is_paid_partnership",
    "media_kind",
    "media_refs",
    "platform",
    "post_id",
    "posted_at",
    "raw_type",
    "shortcode",
    "text",
    "url",
]


def _submodel(annotation: object) -> type[BaseModel] | None:
    """Submodelo expandible: el tipo (desnudando `X | None`) si es un BaseModel, sino None.

    Listas/dicts NO expanden (el DSL del scope no matchea arrays por elemento); solo el campo
    objeto-único anidado (from/sender/engagement) se documenta por sus hijos via dot-notation.
    """
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) != 1:
            return None
        annotation = args[0]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _filterable_paths(model: type[BaseModel]) -> list[str]:
    """Dot-paths del payload tal como los ve el scope (keys JSON: alias incluido, un nivel)."""
    paths: list[str] = []
    for name, field in model.model_fields.items():
        key = field.alias or name
        sub = _submodel(field.annotation)
        if sub is not None:
            paths.extend(f"{key}.{child}" for child in sub.model_fields)
        else:
            paths.append(key)
    return sorted(paths)


def test_email_payload_paths_match_dashboard_reference() -> None:
    assert _filterable_paths(EmailPayload) == EXPECTED_EMAIL_PATHS


def test_telegram_payload_paths_match_dashboard_reference() -> None:
    assert _filterable_paths(TelegramPayload) == EXPECTED_TELEGRAM_PATHS


def test_social_payload_paths_match_dashboard_reference() -> None:
    assert _filterable_paths(SocialPostPayload) == EXPECTED_SOCIAL_PATHS
