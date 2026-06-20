"""Builder de contexto + predicado de skip del resolvedor (`resolve_context`), sin LLM."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.resolve_context import build_email_context, email_needs_resolution


def _exec(sql: str, **p: Any) -> Any:
    with connection() as c:
        r = c.execute(text(sql), p)
        return r.scalar() if r.returns_rows else None


def _source() -> int:
    return int(
        _exec("INSERT INTO sources (user_id, name, type) VALUES (1,'mail','imap') RETURNING id")
    )


def _inbox(src: int, payload: dict[str, Any]) -> int:
    return int(
        _exec(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :s, :e, NOW(), CAST(:p AS JSONB)) RETURNING id",
            s=src,
            e=f"m{payload.get('subject', 'x')}",
            p=json.dumps(payload),
        )
    )


def _identity(kind: str, name: str, *, resolved: bool = False) -> int:
    meta = {"resolved_context_at": "2026-01-01T00:00:00Z"} if resolved else {}
    return int(
        _exec(
            "INSERT INTO mod_identidades (user_id, kind, display_name, source, metadata) "
            "VALUES (1, :k, :n, 'extraction', CAST(:m AS JSONB)) RETURNING id",
            k=kind,
            n=name,
            m=json.dumps(meta),
        )
    )


def _mention(inbox_id: int, identity_id: int, kind: str, *, method: str, email: str | None) -> None:
    _exec(
        "INSERT INTO mod_identidades_mentions "
        "(user_id, source_inbox_ids, mentioned_name, resolved_identity_id, resolved_kind, "
        " resolution_method, email) "
        "VALUES (1, ARRAY[:i], :n, :rid, :rk, :meth, :email)",
        i=inbox_id,
        n="x",
        rid=identity_id,
        rk=kind,
        meth=method,
        email=email,
    )


def _identifier(identity_id: int, kind: str, value_norm: str) -> None:
    _exec(
        "INSERT INTO mod_identidades_identifiers "
        "(user_id, identity_id, platform, kind, value, value_norm, source) "
        "VALUES (1, :id, :k, :k, :v, :v, 'extraction')",
        id=identity_id,
        k=kind,
        v=value_norm,
    )


def test_needs_resolution_when_identity_not_context_resolved() -> None:
    src = _source()
    mid = _inbox(src, {"subject": "hola", "body_text": "Acme firmó"})
    org = _identity("organizacion", "Acme", resolved=False)
    _mention(mid, org, "organizacion", method="exact_name", email=None)
    with connection() as c:
        assert email_needs_resolution(c, 1, mid) is True
        ctx = build_email_context(c, 1, mid)
    assert ctx is not None
    assert ctx.subject == "hola"
    assert {i.identity_id for i in ctx.identities} == {org}


def test_skip_when_all_resolved_and_sender_associated() -> None:
    src = _source()
    mid = _inbox(src, {"subject": "s", "body_text": "b"})
    org = _identity("organizacion", "Acme", resolved=True)
    _identifier(org, "email", "info@acme.com")
    _mention(mid, org, "organizacion", method="sender", email="info@acme.com")
    with connection() as c:
        assert email_needs_resolution(c, 1, mid) is False
        assert build_email_context(c, 1, mid) is None


def test_needs_resolution_when_sender_email_unassociated() -> None:
    # la org del remitente YA está context-resuelta, pero el email del remitente todavía no es
    # identificador de nadie → hay que disponerlo.
    src = _source()
    mid = _inbox(src, {"subject": "s", "body_text": "b"})
    org = _identity("organizacion", "Acme", resolved=True)
    _mention(mid, org, "organizacion", method="sender", email="jobs@acme.com")
    with connection() as c:
        assert email_needs_resolution(c, 1, mid) is True


def test_no_identities_no_resolution() -> None:
    src = _source()
    mid = _inbox(src, {"subject": "s", "body_text": "b"})
    with connection() as c:
        assert email_needs_resolution(c, 1, mid) is False
        assert build_email_context(c, 1, mid) is None
