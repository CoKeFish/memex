"""Almacenamiento y API de WhatsApp con DB real; el cliente neonize se reemplaza por un doble.

Cubre: la migración (tablas), la fuente por cuenta, el catálogo que acumula, la bandeja (poner,
volcar en orden, purgar), la allowlist en `sources.config`, y los endpoints de /cuenta con el
runtime parcheado (pedir código, estado, catálogo, guardar selección con volcado, desvincular).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
from sqlalchemy import text

from memex.db import connection
from memex.ingestors.whatsapp.config import WaAllowedChat
from memex.ingestors.whatsapp.parser import WaMessage
from memex.modules.chat_context import chat_modes_for_source
from memex.whatsapp import store
from memex.whatsapp.runtime import runtime

GROUP = "120363000000000099@g.us"


def _new_account() -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO accounts (user_id, alias, provider, kind, metadata) "
                    "VALUES (1, :alias, 'whatsapp', 'chat', '{}'::jsonb) RETURNING id"
                ),
                {"alias": f"wa-{uuid4().hex[:6]}"},
            ).scalar_one()
        )


def _msg(mid: str, when: datetime, text_: str = "hola") -> WaMessage:
    return WaMessage(
        chat_jid=GROUP,
        message_id=mid,
        from_me=False,
        sender="57300",
        push_name="Ana",
        occurred_at=when,
        message=Message(conversation=text_),
    )


def test_tables_exist() -> None:
    with connection() as c:
        for t in ("whatsapp_links", "whatsapp_chats", "whatsapp_buffer"):
            assert c.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is not None


def test_source_catalog_buffer_and_allowlist() -> None:
    aid = _new_account()
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    with connection() as c:
        src = store.ensure_source(c, 1, aid)
        assert store.ensure_source(c, 1, aid)["id"] == src["id"]  # idempotente
        kind = c.execute(text("SELECT type FROM sources WHERE id = :s"), {"s": src["id"]}).scalar()
        assert kind == "whatsapp"

        store.upsert_chat(
            c,
            1,
            aid,
            GROUP,
            name="",
            kind="group",
            new_messages=2,
            first_at=now,
            last_at=now + timedelta(hours=1),
        )
        store.upsert_chat(
            c,
            1,
            aid,
            GROUP,
            name="Fútbol",
            kind="group",
            new_messages=3,
            first_at=now - timedelta(days=1),
            last_at=now + timedelta(days=1),
        )
        (chat,) = store.list_chats(c, aid, {})
        assert chat["name"] == "Fútbol" and chat["included"] is False
        assert chat["msg_count"] == 0  # lo que HAY: nada en bandeja ni en inbox todavía
        assert chat["first_at"] == now - timedelta(days=1)
        assert chat["last_at"] == now + timedelta(days=1)

        assert store.buffer_put(c, 1, aid, _msg("B", now + timedelta(minutes=5)))
        assert store.buffer_put(c, 1, aid, _msg("A", now))
        assert not store.buffer_put(c, 1, aid, _msg("A", now))  # duplicado
        assert store.buffer_count(c, aid) == 2
        assert store.list_chats(c, aid, {})[0]["msg_count"] == 2  # = bandeja + inbox
        drained = store.buffer_drain(c, aid, GROUP)
        assert [r["message_id"] for r in drained] == ["A", "B"]  # cronológico
        assert store.buffer_count(c, aid) == 0

        assert store.buffer_put(c, 1, aid, _msg("viejo", now))
        c.execute(
            text(
                "UPDATE whatsapp_buffer SET received_at = NOW() - INTERVAL '40 days' "
                "WHERE account_id = :a"
            ),
            {"a": aid},
        )
        assert store.buffer_purge(c, aid, retention_days=30) == 1

        store.save_allowed(c, int(src["id"]), [WaAllowedChat(chat_id=GROUP, mode="intimo")])
        allowed = store.load_allowed(c, int(src["id"]))
        assert allowed[GROUP].mode == "intimo"
        # el contexto de chat lo lee igual que Telegram (clave texto)
        assert chat_modes_for_source(c, int(src["id"])).of({"chat_id": GROUP}) == "intimo"

        store.upsert_link(c, 1, aid, phone="57300", status="codigo", pair_code="ABCD-EFGH")
        store.set_link_status(c, aid, "vinculado", jid="57300", pair_code=None)
        link = store.get_link(c, aid)
        assert link is not None and link["status"] == "vinculado" and link["pair_code"] is None
        assert (aid, 1) in store.linked_accounts(c)


def test_ingest_from_store_lands_in_inbox_once() -> None:
    aid = _new_account()
    now = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    from memex.ingestors.whatsapp.parser import build_payload

    with connection() as c:
        src = store.ensure_source(c, 1, aid)
        wm = _msg("ING1", now, "pago hecho")
        payload = build_payload(wm, chat_title="Fútbol")
        assert payload is not None
        first = store.ingest(c, 1, int(src["id"]), wm, payload, media=[])
        again = store.ingest(c, 1, int(src["id"]), wm, payload, media=[])
        assert (first.inserted, again.inserted) == (True, False)
        assert first.inbox_id is not None and again.inbox_id == first.inbox_id  # el id igual
        row = c.execute(
            text(
                "SELECT payload->>'chat_title' AS titulo, payload->>'text' AS texto FROM inbox "
                "WHERE source_id = :s AND external_id = :e"
            ),
            {"s": src["id"], "e": f"whatsapp:{GROUP}:ING1"},
        ).one()
        assert (row.titulo, row.texto) == ("Fútbol", "pago hecho")


def test_same_message_via_second_source_is_one_row() -> None:
    """Cuenta re-creada (o segundo número en el mismo grupo): el historial no entra dos veces y
    `ingest` devuelve la fila que ya existía en la OTRA fuente (para sumarle el archivo)."""
    from memex.ingestors.whatsapp.parser import build_payload

    now = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    with connection() as c:
        first = store.ensure_source(c, 1, _new_account())
        second = store.ensure_source(c, 1, _new_account())
        wm = _msg(f"DUP-{uuid4().hex[:6]}", now, "misma clase")
        payload = build_payload(wm, chat_title="Curso")
        assert payload is not None
        a = store.ingest(c, 1, int(first["id"]), wm, payload, media=[])
        b = store.ingest(c, 1, int(second["id"]), wm, payload, media=[])
        assert (a.inserted, b.inserted) == (True, False)
        assert b.inbox_id == a.inbox_id
        n = c.execute(
            text("SELECT count(*) FROM inbox WHERE external_id = :e"),
            {"e": f"whatsapp:{GROUP}:{wm.message_id}"},
        ).scalar()
        assert n == 1


class _FakeObjectStore:
    bucket = "test-bucket"

    def __init__(self) -> None:
        self.puts: list[str] = []

    def ensure_bucket(self) -> None:  # pragma: no cover
        pass

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.puts.append(key)

    def get(self, key: str) -> bytes:  # pragma: no cover
        return b""

    def exists(self, key: str) -> bool:  # pragma: no cover
        return True


def test_attach_media_late_requeues_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """El archivo que llega después del texto (historial/bandeja) se adjunta una sola vez y el
    mensaje, ya procesado, vuelve a la cola de extracción (hay una imagen nueva que leer)."""
    import base64
    import hashlib

    from memex.api import ingest_service
    from memex.api.schemas import MediaItem
    from memex.ingestors.whatsapp.parser import build_payload

    fake = _FakeObjectStore()
    monkeypatch.setattr(ingest_service, "get_object_store", lambda: fake)
    aid = _new_account()
    now = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
    data = b"\xff\xd8fake-jpeg"
    item = MediaItem(
        sha256=hashlib.sha256(data).hexdigest(),
        content_type="image/jpeg",
        filename="image.jpg",
        size=len(data),
        data_b64=base64.b64encode(data).decode("ascii"),
    )
    with connection() as c:
        src = store.ensure_source(c, 1, aid)
        wm = _msg("IMG1", now, "")
        wm.message.Clear()
        wm.message.imageMessage.mimetype = "image/jpeg"
        wm.message.imageMessage.fileLength = len(data)
        payload = build_payload(wm, chat_title="Curso")
        assert payload is not None
        got = store.ingest(c, 1, int(src["id"]), wm, payload, media=[])
        assert got.inserted and got.inbox_id is not None
        # ya «procesado» sin el archivo
        c.execute(text("UPDATE inbox SET processed_at = NOW() WHERE id = :i"), {"i": got.inbox_id})
        assert store.has_media(c, got.inbox_id) is False

        assert store.attach_media(c, 1, got.inbox_id, [item]) == 1
        assert store.attach_media(c, 1, got.inbox_id, [item]) == 0  # idempotente
        assert store.attach_media(c, 1, got.inbox_id, []) == 0

        row = c.execute(
            text(
                "SELECT (SELECT count(*) FROM media_assets WHERE inbox_id = i.id) AS n, "
                "(SELECT min(ocr_status) FROM media_assets WHERE inbox_id = i.id) AS st, "
                "processed_at FROM inbox i WHERE i.id = :i"
            ),
            {"i": got.inbox_id},
        ).one()
        assert (int(row.n), row.st, row.processed_at) == (1, "pending", None)
    assert len(fake.puts) == 1


@pytest.fixture
def wa_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", False)
    monkeypatch.setattr(settings, "api_token", "")
    return TestClient(app)


def test_api_flow_with_fake_runtime(wa_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    aid = _new_account()
    calls: list[tuple[str, Any]] = []

    async def fake_pair(user_id: int, account_id: int, phone: str) -> str:
        calls.append(("pair", phone))
        with connection() as c:
            store.upsert_link(
                c, user_id, account_id, phone=phone, status="codigo", pair_code="WXYZ-1234"
            )
        return "WXYZ-1234"

    async def fake_reload(account_id: int) -> list[str]:
        calls.append(("reload", account_id))
        return []

    async def fake_drain(user_id: int, account_id: int, jids: list[str]) -> int:
        calls.append(("drain", jids))
        return 3 if jids else 0

    async def fake_unlink(user_id: int, account_id: int) -> None:
        calls.append(("unlink", account_id))
        with connection() as c:
            store.set_link_status(c, account_id, "cerrado")

    monkeypatch.setattr(runtime, "pair", fake_pair)
    monkeypatch.setattr(runtime, "reload_allowed", fake_reload)
    monkeypatch.setattr(runtime, "drain", fake_drain)
    monkeypatch.setattr(runtime, "unlink", fake_unlink)
    monkeypatch.setattr(runtime, "is_connected", lambda account_id: False)

    base = f"/accounts/{aid}/whatsapp"
    r = wa_client.get(f"{base}/status")
    assert r.status_code == 200 and r.json()["status"] == "sin_vincular"

    r = wa_client.post(f"{base}/pair", json={"phone": "+57 300 111 2233"})
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "WXYZ-1234" and r.json()["phone_masked"] == "…2233"
    assert calls[-1] == ("pair", "573001112233")

    assert wa_client.post(f"{base}/pair", json={"phone": "abc"}).status_code == 422

    r = wa_client.get(f"{base}/status")
    assert r.json()["status"] == "codigo" and r.json()["pair_code"] == "WXYZ-1234"

    with connection() as c:
        store.upsert_chat(
            c,
            1,
            aid,
            GROUP,
            name="Fútbol",
            kind="group",
            new_messages=4,
            first_at=None,
            last_at=None,
        )
    r = wa_client.get(f"{base}/chats")
    assert r.status_code == 200
    (chat,) = r.json()["chats"]
    assert chat["chat_id"] == GROUP and chat["included"] is False and chat["msg_count"] == 0

    r = wa_client.post(
        f"{base}/chats",
        json={
            "allowed": [
                {"chat_id": GROUP, "name": "Fútbol", "mode": "intimo", "since": "2026-01-01"}
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"saved": 1, "newly_included": [GROUP], "ingested": 3}
    assert ("drain", [GROUP]) in calls
    (chat,) = wa_client.get(f"{base}/chats").json()["chats"]
    assert chat["included"] is True and chat["mode"] == "intimo" and chat["since"] == "2026-01-01"

    # guardar de nuevo sin cambios: nada nuevo que volcar
    r = wa_client.post(f"{base}/chats", json={"allowed": [{"chat_id": GROUP, "mode": "intimo"}]})
    assert r.json()["newly_included"] == [] and r.json()["ingested"] == 0

    r = wa_client.post(f"{base}/unlink")
    assert r.status_code == 200 and r.json()["status"] == "cerrado"
    assert wa_client.get(f"{base}/status").json()["status"] == "cerrado"


def test_api_rejects_non_whatsapp_account(wa_client: Any) -> None:
    with connection() as c:
        other = int(
            c.execute(
                text(
                    "INSERT INTO accounts (user_id, alias, provider, kind, metadata) "
                    "VALUES (1, :a, 'telegram', 'chat', '{}'::jsonb) RETURNING id"
                ),
                {"a": f"tg-{uuid4().hex[:6]}"},
            ).scalar_one()
        )
    assert wa_client.get(f"/accounts/{other}/whatsapp/status").status_code == 422
    assert wa_client.get("/accounts/999999/whatsapp/status").status_code == 404


def test_group_description_is_kept_and_ingested_once_per_version() -> None:
    aid = _new_account()
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with connection() as c:
        src = store.ensure_source(c, 1, aid)
        store.upsert_chat(
            c,
            1,
            aid,
            GROUP,
            name="B1.1",
            kind="group",
            new_messages=0,
            first_at=None,
            last_at=None,
            description="Slides: https://drive.google.com/x  Reglas del curso",
        )
        assert store.chat_description(c, aid, GROUP).startswith("Slides:")
        # None no toca; '' borra
        store.upsert_chat(
            c, 1, aid, GROUP, name="", kind="group", new_messages=1, first_at=now, last_at=now
        )
        assert store.chat_description(c, aid, GROUP).startswith("Slides:")
        desc = store.chat_description(c, aid, GROUP)
        a = store.ingest_description(
            c, 1, int(src["id"]), jid=GROUP, name="B1.1", description=desc, when=now
        )
        b = store.ingest_description(
            c, 1, int(src["id"]), jid=GROUP, name="B1.1", description=desc, when=now
        )
        assert a.inserted is True and b.inserted is False and b.inbox_id == a.inbox_id
        row = c.execute(
            text(
                "SELECT payload->>'text' AS texto, "
                "payload->'sender'->>'display_name' AS remitente, "
                "external_id FROM inbox WHERE id = :i"
            ),
            {"i": a.inbox_id},
        ).one()
        assert "drive.google.com/x" in row.texto and row.remitente == "descripción del grupo"
        assert row.external_id.startswith(f"whatsapp:{GROUP}:descripcion:")
        # versión nueva → otra fila
        c2 = store.ingest_description(
            c, 1, int(src["id"]), jid=GROUP, name="B1.1", description=desc + " v2", when=now
        )
        assert c2.inserted is True and c2.inbox_id != a.inbox_id
        assert (
            store.ingest_description(
                c, 1, int(src["id"]), jid=GROUP, name="B1.1", description="   ", when=now
            ).inserted
            is False
        )
        store.upsert_chat(
            c,
            1,
            aid,
            GROUP,
            name="",
            kind="group",
            new_messages=0,
            first_at=None,
            last_at=None,
            description="",
        )
        assert store.chat_description(c, aid, GROUP) == ""
