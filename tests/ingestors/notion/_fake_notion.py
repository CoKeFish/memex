"""Un Notion de juguete detrás de `httpx.MockTransport`: páginas con padre, bloques (anidados),
bases de datos, usuario. Sirve para probar cliente, parser y source sin red."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


def _rt(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "plain_text": text, "text": {"content": text}}]


def block(
    btype: str,
    text: str = "",
    *,
    bid: str | None = None,
    children: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Bloque de la API con `rich_text` y, opcionalmente, hijos (se sirven en /children)."""
    data: dict[str, Any] = {"rich_text": _rt(text)} if text or btype not in ("divider",) else {}
    data.update(extra)
    b: dict[str, Any] = {
        "object": "block",
        "id": bid or f"blk-{abs(hash((btype, text, json.dumps(extra, sort_keys=True)))) % 10**8}",
        "type": btype,
        "has_children": bool(children),
        btype: data,
    }
    b["_children"] = children or []
    return b


def page(
    pid: str,
    title: str,
    *,
    parent: tuple[str, str | None] = ("workspace", None),
    edited: str = "2026-09-01T10:00:00.000Z",
    created: str = "2026-08-01T10:00:00.000Z",
    blocks: list[dict[str, Any]] | None = None,
    archived: bool = False,
) -> dict[str, Any]:
    ptype, parent_id = parent
    par: dict[str, Any] = {"type": ptype}
    if parent_id is not None:
        par[ptype] = parent_id
    return {
        "object": "page",
        "id": pid,
        "created_time": created,
        "last_edited_time": edited,
        "archived": archived,
        "in_trash": archived,
        "parent": par,
        "url": f"https://www.notion.so/{pid.replace('-', '')}",
        "properties": {"title": {"id": "title", "type": "title", "title": _rt(title)}},
        "_blocks": blocks or [],
    }


def database(did: str, title: str, *, parent: tuple[str, str | None]) -> dict[str, Any]:
    ptype, parent_id = parent
    par: dict[str, Any] = {"type": ptype}
    if parent_id is not None:
        par[ptype] = parent_id
    return {"object": "database", "id": did, "title": _rt(title), "parent": par}


@dataclass
class FakeNotion:
    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    databases: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail_429_once: bool = False
    page_size: int = 100
    shared: set[str] | None = None  # ids visibles; None = todas

    def add(self, *pages: dict[str, Any]) -> None:
        for p in pages:
            self.pages[p["id"]] = p

    def _blocks_index(self) -> dict[str, list[dict[str, Any]]]:
        idx: dict[str, list[dict[str, Any]]] = {}

        def walk(owner: str, blocks: list[dict[str, Any]]) -> None:
            idx[owner] = blocks
            for b in blocks:
                walk(b["id"], b.get("_children", []))

        for p in self.pages.values():
            walk(p["id"], p.get("_blocks", []))
        return idx

    @staticmethod
    def _public(obj: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in obj.items() if not k.startswith("_")}

    def _visible(self, pid: str) -> bool:
        return self.shared is None or pid in self.shared

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _paged(self, items: list[dict[str, Any]], cursor: str | None) -> dict[str, Any]:
        start = int(cursor or 0)
        chunk = items[start : start + self.page_size]
        more = start + self.page_size < len(items)
        return {
            "object": "list",
            "results": chunk,
            "has_more": more,
            "next_cursor": str(start + self.page_size) if more else None,
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        assert request.headers.get("Notion-Version"), "falta Notion-Version"
        if self.fail_429_once:
            self.fail_429_once = False
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow"})
        if path == "/v1/users/me":
            return httpx.Response(200, json={"object": "user", "id": "bot-1", "name": "memex"})
        if path == "/v1/search":
            body = json.loads(request.content or b"{}")
            visible = [
                self._public(p)
                for p in self.pages.values()
                if self._visible(p["id"]) and not p.get("archived")
            ]
            visible.sort(key=lambda p: p["last_edited_time"], reverse=True)
            return httpx.Response(200, json=self._paged(visible, body.get("start_cursor")))
        if path.startswith("/v1/pages/"):
            pid = path.rsplit("/", 1)[-1]
            p = self.pages.get(pid)
            if p is None or not self._visible(pid):
                return httpx.Response(
                    404, json={"message": "Could not find page", "code": "object_not_found"}
                )
            return httpx.Response(200, json=self._public(p))
        if path.startswith("/v1/databases/"):
            did = path.rsplit("/", 1)[-1]
            d = self.databases.get(did)
            if d is None:
                return httpx.Response(404, json={"message": "Could not find database"})
            return httpx.Response(200, json=d)
        if path.startswith("/v1/blocks/") and path.endswith("/children"):
            bid = path.split("/")[3]
            blocks = self._blocks_index().get(bid, [])
            cursor = request.url.params.get("start_cursor")
            return httpx.Response(200, json=self._paged([self._public(b) for b in blocks], cursor))
        return httpx.Response(404, json={"message": f"no route {path}"})
