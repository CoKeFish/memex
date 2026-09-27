"""NotionSource — `Source[NotesCursor]` (polling) sobre la API de Notion.

Qué entra: las páginas raíz configuradas (`roots`) y sus subpáginas hasta `max_depth`; con
`roots=[]`, todo lo compartido con la integración. Cómo se detectan novedades: la búsqueda de
Notion ordenada por `last_edited_time` descendente se recorre hasta la marca de agua del cursor
(menos una holgura); cada página más nueva que lo que el cursor recuerda de ella se relee
(metadatos + bloques, con recursión en los anidados) y sale como UN `SourceRecord` con
`external_id = notion:<page_id>` y `occurred_at = last_edited_time`. El borde de ingesta reemplaza
el payload si la página ya existía (edición) y la marca para reprocesar.

Orden de salida ascendente por edición y tope `max_pages_per_run`: así `advance_checkpoint` sube la
marca de agua solo hasta lo realmente flusheado y lo que quedó afuera entra en la corrida siguiente.
"""

from __future__ import annotations

import asyncio
from builtins import type as _type
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from memex.core.cursors import NotesCursor, PageState
from memex.core.payloads import BasePayload, NotePayload
from memex.core.source import (
    HEALTH_CODE_REAUTH_REQUIRED,
    HealthResult,
    Source,
    SourceKind,
    SourceRecord,
)
from memex.ingestors.notion.client import NotionApiError, NotionClient
from memex.ingestors.notion.config import NotionConfig
from memex.ingestors.notion.parser import (
    build_payload,
    page_title,
    parent_ref,
    parse_time,
    render_blocks,
    rich_text,
)
from memex.logging import get_logger

#: Bloques cuyos hijos se recorren al MISMO nivel (son layout, no jerarquía de contenido).
_FLAT_CONTAINERS = frozenset({"table", "column_list", "column", "synced_block"})
#: Bloques cuyos hijos NO se expanden acá (son páginas/bases aparte).
_PAGE_LIKE = frozenset({"child_page", "child_database"})


class NotionSource:
    type: ClassVar[str] = "notion"
    kind: ClassVar[SourceKind] = SourceKind.NOTES
    payload_schema: ClassVar[_type[BasePayload]] = NotePayload
    config_schema: ClassVar[_type[BaseModel]] = NotionConfig
    checkpoint_schema: ClassVar[_type[BaseModel]] = NotesCursor

    def __init__(self, cfg: NotionConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.cfg = cfg
        self._transport = transport  # inyectable en tests (respx/MockTransport)
        self._log = get_logger("memex.ingestors.notion.source", roots=len(cfg.roots))

    def _client(self) -> NotionClient:
        return NotionClient(
            self.cfg.token.get_secret_value(),
            version=self.cfg.notion_version,
            requests_per_second=self.cfg.requests_per_second,
            timeout_s=self.cfg.timeout_s,
            transport=self._transport,
        )

    # ------------------------------------------------------------------ salud
    async def health_check(self) -> HealthResult:
        def _probe() -> HealthResult:
            now = datetime.now(UTC)
            try:
                with self._client() as client:
                    me = client.me()
                name = str(me.get("name") or me.get("id") or "integración")
                return HealthResult(
                    status="healthy", detail=f"integración «{name}» autorizada", checked_at=now
                )
            except NotionApiError as e:
                if e.status_code in (401, 403):
                    return HealthResult(
                        status="unhealthy",
                        detail=(
                            "El token de la integración de Notion no es válido o fue revocado; "
                            f"volvé a pegarlo en la cuenta. (detalle técnico: {e})"
                        ),
                        checked_at=now,
                        code=HEALTH_CODE_REAUTH_REQUIRED,
                    )
                return HealthResult(status="unhealthy", detail=str(e), checked_at=now)
            except Exception as e:  # red, DNS…
                return HealthResult(
                    status="unhealthy", detail=f"{type(e).__name__}: {e}", checked_at=now
                )

        return await asyncio.to_thread(_probe)

    # ------------------------------------------------------------------ fetch
    def fetch(self, checkpoint: NotesCursor) -> Iterable[SourceRecord]:
        with self._client() as client:
            candidates = self._candidates(client, checkpoint)
            candidates.sort(key=lambda p: (parse_time(p.get("last_edited_time")), str(p.get("id"))))
            cache: dict[str, dict[str, Any]] = {}
            yielded = skipped = 0
            for page in candidates:
                if yielded >= self.cfg.max_pages_per_run:
                    self._log.info(
                        "notion.fetch.capped",
                        candidates=len(candidates),
                        cap=self.cfg.max_pages_per_run,
                    )
                    break
                ancestry = self._ancestry(client, page, cache)
                if ancestry is None:
                    skipped += 1
                    continue
                root_id, path = ancestry
                blocks: list[dict[str, Any]] = []
                self._flatten(client, str(page["id"]), 0, blocks)
                rendered = render_blocks(blocks, max_blocks=self.cfg.max_blocks_per_page)
                payload = build_payload(page, rendered, path=path, root_id=root_id)
                yielded += 1
                yield SourceRecord(
                    external_id=f"notion:{payload.page_id}",
                    occurred_at=payload.last_edited_time,
                    payload=payload.model_dump(mode="json", by_alias=True),
                    dedupe_keys=[f"notion:{payload.page_id}"],
                )
            self._log.info(
                "notion.fetch.end",
                candidates=len(candidates),
                yielded=yielded,
                outside_roots=skipped,
                api_calls=client.calls,
            )

    def _candidates(self, client: NotionClient, cp: NotesCursor) -> list[dict[str, Any]]:
        """Páginas nuevas o editadas desde el cursor (búsqueda desc por edición hasta la marca)."""
        floor = (
            cp.watermark - timedelta(seconds=self.cfg.watermark_slack_s)
            if cp.watermark is not None
            else None
        )
        out: list[dict[str, Any]] = []
        for page in client.search_pages_by_edit():
            if page.get("object") != "page":
                continue
            edited = parse_time(page.get("last_edited_time"))
            if floor is not None and edited < floor:
                break
            if page.get("archived") or page.get("in_trash"):
                continue
            state = cp.pages.get(str(page.get("id")))
            if state is not None and edited <= state.last_edited_time:
                continue
            out.append(page)
        return out

    def _ancestry(
        self, client: NotionClient, page: dict[str, Any], cache: dict[str, dict[str, Any]]
    ) -> tuple[str | None, list[str]] | None:
        """(raíz, títulos de los ancestros desde la raíz hasta el padre) o None si la página no
        cuelga de una raíz configurada (o excede `max_depth`)."""
        roots = set(self.cfg.roots)
        titles: list[str] = []
        cur: dict[str, Any] = page
        cur_id = str(page.get("id"))
        depth = 0
        while True:
            if cur_id in roots:
                return cur_id, titles
            ptype, pid = parent_ref(cur)
            if ptype == "workspace" or pid is None:
                # Llegamos al tope: solo vale si no hay raíces configuradas (todo lo compartido).
                return (None, titles) if not roots else None
            depth += 1
            if depth > self.cfg.max_depth:
                return None
            parent = cache.get(pid)
            if parent is None:
                try:
                    if ptype == "database_id":
                        parent = client.database(pid)
                    elif ptype == "block_id":
                        parent = client.block(pid)
                    else:
                        parent = client.page(pid)
                except NotionApiError as e:
                    # 404: el ancestro no está compartido con la integración → fuera de alcance.
                    self._log.info(
                        "notion.ancestry.unreachable", page_id=cur_id, parent=pid, error=str(e)
                    )
                    return None
                cache[pid] = parent
            if ptype != "block_id":  # los bloques no aportan título a la ruta
                titles.insert(0, _title_of(parent))
            cur, cur_id = parent, pid

    def _flatten(
        self, client: NotionClient, block_id: str, depth: int, out: list[dict[str, Any]]
    ) -> None:
        for block in client.block_children(block_id):
            if len(out) >= self.cfg.max_blocks_per_page + 1:
                return  # el render marca `truncated` al pasarse del tope
            block["_depth"] = depth
            out.append(block)
            btype = str(block.get("type") or "")
            if block.get("has_children") and btype not in _PAGE_LIKE:
                child_depth = depth if btype in _FLAT_CONTAINERS else depth + 1
                self._flatten(client, str(block["id"]), child_depth, out)

    # ------------------------------------------------------------------ cursor
    def advance_checkpoint(self, checkpoint: NotesCursor, last: SourceRecord) -> NotesCursor:
        page_id = str(last.payload.get("page_id") or last.external_id.split(":", 1)[-1])
        edited = parse_time(last.payload.get("last_edited_time"))
        pages = dict(checkpoint.pages)
        pages[page_id] = PageState(last_edited_time=edited)
        watermark = edited if checkpoint.watermark is None else max(checkpoint.watermark, edited)
        return NotesCursor(pages=pages, watermark=watermark)


def _title_of(obj: dict[str, Any]) -> str:
    if obj.get("object") == "database":
        return rich_text(obj.get("title")).strip() or "(base de datos)"
    if obj.get("object") == "block":
        return ""
    return page_title(obj)


def make_source(cfg: dict[str, Any], env: Mapping[str, str] | None = None) -> Source[Any]:
    """SourceFactory de Notion: valida el config dict y devuelve `NotionSource`."""
    return NotionSource(NotionConfig.from_source_config(cfg, env))
