"""Refetch de un mensaje (memex.refetch): re-traer por UID y reemplazar el payload.

El fetcher IMAP real se inyecta como stub: acá se prueba el contrato (parse del external_id,
reemplazo del payload, errores tipados), no la red.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from memex.core.source import SourceRecord
from memex.db import connection
from memex.refetch import RefetchError, parse_imap_external_id, refetch_inbox

_EXT = "imap:imap.gmail.com:1:77845"


def _seed(source_type: str = "imap", ext: str = _EXT) -> int:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type, config) "
                "VALUES (1, :n, :t, CAST('{}' AS JSONB)) RETURNING id"
            ),
            {"n": f"rf-{source_type}-{ext}", "t": source_type},
        ).scalar()
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST(:payload AS JSONB))
                RETURNING id
                """
            ),
            {
                "sid": sid,
                "eid": ext,
                "occ": datetime(2026, 9, 4, 22, 0, tzinfo=UTC),
                "payload": '{"subject": "Player32", "body_text": "", "body_source": "text"}',
            },
        ).scalar()
    assert iid is not None
    return int(iid)


def _record(ext: str = _EXT, body: str = "Player32 is open: submit your game.") -> SourceRecord:
    return SourceRecord(
        external_id=ext,
        occurred_at=datetime(2026, 9, 4, 22, 0, tzinfo=UTC),
        payload={"subject": "Player32", "body_text": body, "body_source": "text"},
        dedupe_keys=[ext],
    )


def test_parse_imap_external_id() -> None:
    ref = parse_imap_external_id(_EXT)
    assert (ref.server, ref.uidvalidity, ref.uid) == ("imap.gmail.com", 1, 77845)
    with pytest.raises(RefetchError) as e:
        parse_imap_external_id("telegram:123")
    assert e.value.code == "bad_external_id"


def test_refetch_replaces_payload_and_reports_sizes() -> None:
    iid = _seed()
    seen: list[tuple[int, int]] = []

    def fetcher(uidvalidity: int, uid: int) -> SourceRecord | None:
        seen.append((uidvalidity, uid))
        return _record()

    res = refetch_inbox(1, iid, fetcher=fetcher)
    assert seen == [(1, 77845)]
    assert (res.body_before, res.body_after, res.changed) == (0, 35, True)
    with connection() as c:
        body = c.execute(
            text("SELECT payload->>'body_text' FROM inbox WHERE id = :id"), {"id": iid}
        ).scalar_one()
    assert body == "Player32 is open: submit your game."


def test_refetch_errors_are_typed() -> None:
    iid = _seed()
    with pytest.raises(RefetchError) as gone:
        refetch_inbox(1, iid, fetcher=lambda _v, _u: None)
    assert gone.value.code == "gone"

    with pytest.raises(RefetchError) as mismatch:
        refetch_inbox(1, iid, fetcher=lambda _v, _u: _record(ext="imap:imap.gmail.com:1:1"))
    assert mismatch.value.code == "mismatch"

    with pytest.raises(RefetchError) as missing:
        refetch_inbox(1, 999_999_999, fetcher=lambda _v, _u: _record())
    assert missing.value.code == "not_found"

    other = _seed(source_type="telegram", ext="tg:1:2")
    with pytest.raises(RefetchError) as unsupported:
        refetch_inbox(1, other, fetcher=lambda _v, _u: _record())
    assert unsupported.value.code == "unsupported"


def test_refetch_endpoint_maps_errors(client: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """El endpoint corre el refetch real con el fetcher IMAP reemplazado por un stub; los errores
    tipados salen como HTTP (fuente no soportada → 422, inbox ajeno → 404)."""
    import memex.refetch as rf

    monkeypatch.setattr(rf, "_imap_fetcher", lambda *_a, **_k: lambda _v, _u: _record())
    iid = _seed()
    r = client.post(f"/inbox/{iid}/refetch")  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    assert (r.json()["body_before"], r.json()["body_after"]) == (0, 35)

    other = _seed(source_type="telegram", ext="tg:9:9")
    r = client.post(f"/inbox/{other}/refetch")  # type: ignore[attr-defined]
    assert r.status_code == 422
    r = client.post("/inbox/999999999/refetch")  # type: ignore[attr-defined]
    assert r.status_code == 404
