"""NotionSource contra un Notion de juguete: alcance por raíces, texto de bloques, cursor por
edición (solo lo nuevo/editado vuelve a salir), tope por corrida, salud."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from memex.core.cursors import NotesCursor
from memex.core.source import HEALTH_CODE_REAUTH_REQUIRED, SourceConfigError, SourceRecord
from memex.ingestors.notion.client import NotionClient
from memex.ingestors.notion.config import NotionConfig, normalize_page_id
from memex.ingestors.notion.parser import render_blocks
from memex.ingestors.notion.source import NotionSource, make_source
from tests.ingestors.notion._fake_notion import FakeNotion, block, database, page

ROOT = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
GRANDCHILD = "33333333-3333-3333-3333-333333333333"
OUTSIDE = "44444444-4444-4444-4444-444444444444"
DB = "55555555-5555-5555-5555-555555555555"
IN_DB = "66666666-6666-6666-6666-666666666666"


def _cfg(**kw: object) -> NotionConfig:
    base: dict[str, object] = {
        "token": SecretStr("secret"),
        "roots": [ROOT],
        "requests_per_second": 0,
    }
    base.update(kw)
    return NotionConfig(**base)


def _workspace() -> FakeNotion:
    fake = FakeNotion()
    fake.add(
        page(
            ROOT,
            "Proyectos",
            edited="2026-09-01T10:00:00.000Z",
            blocks=[
                block("heading_1", "Ideas"),
                block("paragraph", "Primera idea."),
                block(
                    "bulleted_list_item",
                    "Lista madre",
                    children=[
                        block("bulleted_list_item", "Hija"),
                        block("to_do", "Hacer", checked=True),
                    ],
                ),
                block("child_page", bid=CHILD, title="memex"),
            ],
        ),
        page(
            CHILD,
            "memex",
            parent=("page_id", ROOT),
            edited="2026-09-02T10:00:00.000Z",
            blocks=[
                block("paragraph", "Notas del proyecto"),
                block("child_page", bid=GRANDCHILD, title="Diario"),
            ],
        ),
        page(
            GRANDCHILD,
            "Diario",
            parent=("page_id", CHILD),
            edited="2026-09-03T10:00:00.000Z",
            blocks=[block("numbered_list_item", "uno"), block("numbered_list_item", "dos")],
        ),
        page(
            OUTSIDE,
            "Recetas",
            edited="2026-09-04T10:00:00.000Z",
            blocks=[block("paragraph", "Arepas")],
        ),
        page(IN_DB, "Tarea X", parent=("database_id", DB), edited="2026-09-05T10:00:00.000Z"),
    )
    fake.databases[DB] = database(DB, "Tablero", parent=("page_id", ROOT))
    return fake


def _records(src: NotionSource, cp: NotesCursor) -> list[SourceRecord]:
    return list(src.fetch(cp))


def test_fetch_scopes_to_roots_and_renders_text() -> None:
    fake = _workspace()
    src = NotionSource(_cfg(), transport=fake.transport())
    recs = _records(src, NotesCursor())
    ids = [r.payload["page_id"] for r in recs]
    # Ascendente por edición; «Recetas» (fuera de la raíz) NO entra; la página de la base sí.
    assert ids == [ROOT, CHILD, GRANDCHILD, IN_DB]
    root = recs[0]
    assert root.external_id == f"notion:{ROOT}" and root.dedupe_keys == [f"notion:{ROOT}"]
    assert root.occurred_at == datetime(2026, 9, 1, 10, tzinfo=UTC)
    body = root.payload["body_text"]
    assert body.splitlines() == [
        "# Ideas",
        "Primera idea.",
        "- Lista madre",
        "  - Hija",
        "  [x] Hacer",
        "📄 memex",
    ]
    assert root.payload["child_page_ids"] == [CHILD]
    assert root.payload["path"] == [] and root.payload["root_id"] == ROOT
    grand = recs[2]
    assert grand.payload["path"] == ["Proyectos", "memex"]
    assert grand.payload["body_text"] == "1. uno\n2. dos"
    assert grand.payload["parent_id"] == CHILD and grand.payload["title"] == "Diario"
    in_db = recs[3]
    assert in_db.payload["path"] == ["Proyectos", "Tablero"]
    assert in_db.payload["provider"] == "notion" and in_db.payload["url"].startswith("https://")


def test_cursor_only_new_or_edited_pages_come_back() -> None:
    fake = _workspace()
    src = NotionSource(_cfg(), transport=fake.transport())
    cp = NotesCursor()
    recs = _records(src, cp)
    for r in recs:
        cp = src.advance_checkpoint(cp, r)
    assert cp.watermark == datetime(2026, 9, 5, 10, tzinfo=UTC)
    assert set(cp.pages) == {ROOT, CHILD, GRANDCHILD, IN_DB}
    # Sin cambios → nada.
    assert _records(src, cp) == []
    # Editan «memex» → solo esa vuelve, con el texto nuevo.
    fake.pages[CHILD]["last_edited_time"] = "2026-09-06T08:00:00.000Z"
    fake.pages[CHILD]["_blocks"] = [block("paragraph", "Texto editado")]
    again = _records(src, cp)
    assert [r.payload["page_id"] for r in again] == [CHILD]
    assert again[0].payload["body_text"] == "Texto editado"
    assert again[0].external_id == f"notion:{CHILD}"  # mismo registro: reemplaza, no duplica
    cp2 = src.advance_checkpoint(cp, again[0])
    assert cp2.pages[CHILD].last_edited_time == datetime(2026, 9, 6, 8, tzinfo=UTC)
    assert cp2.watermark == datetime(2026, 9, 6, 8, tzinfo=UTC)
    # Una página fuera de la raíz editada después no produce nada.
    fake.pages[OUTSIDE]["last_edited_time"] = "2026-09-07T08:00:00.000Z"
    assert _records(src, cp2) == []


def test_cap_per_run_takes_oldest_first_and_watermark_follows() -> None:
    fake = _workspace()
    src = NotionSource(_cfg(max_pages_per_run=2), transport=fake.transport())
    cp = NotesCursor()
    first = _records(src, cp)
    assert [r.payload["page_id"] for r in first] == [ROOT, CHILD]
    for r in first:
        cp = src.advance_checkpoint(cp, r)
    second = _records(src, cp)
    assert [r.payload["page_id"] for r in second] == [GRANDCHILD, IN_DB]


def test_roots_empty_means_everything_shared() -> None:
    fake = _workspace()
    src = NotionSource(_cfg(roots=[]), transport=fake.transport())
    ids = {r.payload["page_id"] for r in _records(src, NotesCursor())}
    assert OUTSIDE in ids and ROOT in ids
    outside = next(r for r in _records(src, NotesCursor()) if r.payload["page_id"] == OUTSIDE)
    assert outside.payload["root_id"] is None and outside.payload["path"] == []


def test_unshared_ancestor_puts_page_out_of_scope() -> None:
    fake = _workspace()
    fake.shared = {CHILD, GRANDCHILD}  # la raíz no está compartida con la integración
    src = NotionSource(_cfg(), transport=fake.transport())
    assert _records(src, NotesCursor()) == []


def test_health_check_and_reauth_code() -> None:
    fake = _workspace()
    src = NotionSource(_cfg(), transport=fake.transport())
    ok = asyncio.run(src.health_check())
    assert ok.status == "healthy" and "memex" in ok.detail

    import httpx

    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"message": "API token is invalid.", "code": "unauthorized"}
        )

    bad = NotionSource(_cfg(), transport=httpx.MockTransport(unauthorized))
    res = asyncio.run(bad.health_check())
    assert res.status == "unhealthy" and res.code == HEALTH_CODE_REAUTH_REQUIRED


def test_client_retries_429_and_paginates() -> None:
    fake = _workspace()
    fake.page_size = 2
    fake.fail_429_once = True
    with NotionClient(
        "t", version="2022-06-28", requests_per_second=0, transport=fake.transport()
    ) as c:
        pages = list(c.search_pages_by_edit())
    assert len(pages) == 5
    assert fake.calls.count("POST /v1/search") == 4  # 1 reintento + 3 páginas de 2


def test_config_and_factory() -> None:
    assert normalize_page_id(
        "https://www.notion.so/Mi-pagina-1111111111111111111111111111111a"
    ) == ("11111111-1111-1111-1111-11111111111a")
    assert normalize_page_id("nope") is None
    cfg = NotionConfig.from_source_config(
        {"roots": [ROOT, ROOT.replace("-", "")], "max_depth": 3}, {"MEMEX_NOTION_TOKEN": "secret"}
    )
    assert cfg.roots == [ROOT] and cfg.max_depth == 3
    with pytest.raises(SourceConfigError):
        NotionConfig.from_source_config({"roots": [ROOT]}, {})
    with pytest.raises(SourceConfigError):
        NotionConfig.from_source_config({"roots": ["x"]}, {"MEMEX_NOTION_TOKEN": "s"})
    src = make_source({"roots": [ROOT]}, {"MEMEX_NOTION_TOKEN": "s"})
    assert isinstance(src, NotionSource) and src.type == "notion" and src.kind.value == "notes"


def test_render_blocks_misc_types() -> None:
    blocks = [
        {"type": "quote", "quote": {"rich_text": [{"plain_text": "cita"}]}, "_depth": 0},
        {
            "type": "code",
            "code": {"rich_text": [{"plain_text": "x = 1"}], "language": "python"},
            "_depth": 0,
        },
        {"type": "divider", "divider": {}, "_depth": 0},
        {
            "type": "table_row",
            "table_row": {"cells": [[{"plain_text": "a"}], [{"plain_text": "b"}]]},
            "_depth": 0,
        },
        {"type": "bookmark", "bookmark": {"url": "https://x.y", "caption": []}, "_depth": 0},
        {"type": "image", "image": {"caption": [{"plain_text": "foto"}]}, "_depth": 0},
        {"type": "child_database", "child_database": {"title": "Tablero"}, "_depth": 0},
        {
            "type": "callout",
            "callout": {"rich_text": [{"plain_text": "ojo"}], "icon": {"emoji": "💡"}},
            "_depth": 1,
        },
    ]
    r = render_blocks(blocks, max_blocks=100)
    assert r.lines == [
        "> cita",
        "```python\nx = 1\n```",
        "---",
        "a | b",
        "https://x.y",
        "[imagen: foto]",
        "[base de datos: Tablero]",
        "  > 💡 ojo",
    ]
    assert render_blocks(blocks, max_blocks=3).truncated is True
