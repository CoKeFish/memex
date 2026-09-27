"""Bloques de Notion → texto llano estilo markdown + metadatos de página.

Determinista y sin red: recibe los objetos JSON de la API (página, lista de bloques ya recorrida
por el cliente) y arma `NotePayload`. Los bloques anidados (listas, columnas, toggles, tablas) los
trae el cliente por recursión; las subpáginas (`child_page`) NO se expanden acá: se listan como
referencia y el source las recorre como registros propios.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from memex.core.payloads import NotePayload


@dataclass
class RenderedBlocks:
    lines: list[str] = field(default_factory=list)
    child_pages: list[tuple[str, str]] = field(default_factory=list)  # (id, título)
    truncated: bool = False


def rich_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(str(p.get("plain_text") or "") for p in parts if isinstance(p, dict))


def page_title(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                t = rich_text(prop.get("title")).strip()
                if t:
                    return t
    # Páginas dentro de bases de datos sin propiedad título visible, o wiki sin título.
    return "(sin título)"


def parse_time(value: Any) -> datetime:
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.now(UTC)


def parent_ref(obj: dict[str, Any]) -> tuple[str, str | None]:
    """(`page_id`|`database_id`|`block_id`|`workspace`, id) del padre de una página/base."""
    parent = obj.get("parent") or {}
    ptype = str(parent.get("type") or "workspace")
    if ptype == "workspace":
        return ptype, None
    return ptype, (str(parent.get(ptype)) if parent.get(ptype) else None)


def _line(block: dict[str, Any], depth: int, counter: dict[str, int]) -> str | None:
    btype = str(block.get("type") or "")
    data = block.get(btype) or {}
    if not isinstance(data, dict):
        return None
    indent = "  " * depth
    text = rich_text(data.get("rich_text"))
    if btype == "paragraph":
        return indent + text if text else ""
    if btype in ("heading_1", "heading_2", "heading_3"):
        level = int(btype[-1])
        return f"{indent}{'#' * level} {text}"
    if btype == "bulleted_list_item":
        return f"{indent}- {text}"
    if btype == "numbered_list_item":
        counter[str(depth)] = counter.get(str(depth), 0) + 1
        return f"{indent}{counter[str(depth)]}. {text}"
    if btype == "to_do":
        mark = "x" if data.get("checked") else " "
        return f"{indent}[{mark}] {text}"
    if btype == "toggle":
        return f"{indent}▸ {text}"
    if btype == "quote":
        return f"{indent}> {text}"
    if btype == "callout":
        icon = data.get("icon") or {}
        emoji = str(icon.get("emoji") or "") if isinstance(icon, dict) else ""
        return f"{indent}> {emoji + ' ' if emoji else ''}{text}"
    if btype == "code":
        lang = str(data.get("language") or "")
        return f"{indent}```{lang}\n{text}\n{indent}```"
    if btype == "equation":
        return f"{indent}$ {data.get('expression') or ''} $"
    if btype == "divider":
        return f"{indent}---"
    if btype == "table_row":
        cells = data.get("cells") or []
        return indent + " | ".join(rich_text(c) for c in cells if isinstance(c, list))
    if btype in ("bookmark", "embed", "link_preview"):
        url = str(data.get("url") or "")
        caption = rich_text(data.get("caption"))
        return f"{indent}{caption + ': ' if caption else ''}{url}".rstrip(": ")
    if btype in ("image", "video", "file", "pdf", "audio"):
        caption = rich_text(data.get("caption"))
        name = str(data.get("name") or "")
        label = {"image": "imagen", "video": "video", "audio": "audio"}.get(btype, "archivo")
        extra = caption or name
        return f"{indent}[{label}{': ' + extra if extra else ''}]"
    if btype == "child_database":
        return f"{indent}[base de datos: {data.get('title') or ''}]"
    if btype in ("table", "column_list", "column", "synced_block", "table_of_contents"):
        return None  # contenedores: solo sus hijos
    if btype == "child_page":
        return f"{indent}📄 {data.get('title') or ''}"
    if btype in ("breadcrumb", "template", "link_to_page", "unsupported"):
        return None
    return indent + text if text else None


def render_blocks(blocks: list[dict[str, Any]], *, max_blocks: int) -> RenderedBlocks:
    """`blocks` ya viene APLANADO por el source: cada bloque trae `_depth` (nivel de anidamiento)
    y los hijos siguen a su padre. Devuelve las líneas y las subpáginas encontradas."""
    out = RenderedBlocks()
    counters: dict[str, int] = {}
    seen = 0
    last_type = ""
    for block in blocks:
        if seen >= max_blocks:
            out.truncated = True
            break
        seen += 1
        btype = str(block.get("type") or "")
        depth = int(block.get("_depth") or 0)
        if btype != "numbered_list_item" or last_type != "numbered_list_item":
            counters = {} if btype != "numbered_list_item" else counters
        if btype == "child_page":
            data = block.get("child_page") or {}
            out.child_pages.append((str(block.get("id")), str(data.get("title") or "")))
        line = _line(block, depth, counters)
        last_type = btype
        if line is None:
            continue
        out.lines.append(line)
    return out


def build_payload(
    page: dict[str, Any],
    rendered: RenderedBlocks,
    *,
    path: list[str],
    root_id: str | None,
) -> NotePayload:
    ptype, pid = parent_ref(page)
    body = "\n".join(rendered.lines).strip()
    # Colapsa corridas de más de dos líneas vacías (párrafos vacíos de Notion).
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return NotePayload(
        provider="notion",
        page_id=str(page.get("id")),
        title=page_title(page),
        body_text=body,
        url=str(page.get("url")) if page.get("url") else None,
        path=list(path),
        root_id=root_id,
        parent_id=pid if ptype in ("page_id", "database_id", "block_id") else None,
        created_time=parse_time(page.get("created_time")),
        last_edited_time=parse_time(page.get("last_edited_time")),
        archived=bool(page.get("archived") or page.get("in_trash")),
        body_truncated=rendered.truncated,
        child_page_ids=[cid for cid, _ in rendered.child_pages],
    )
