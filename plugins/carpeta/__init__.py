"""Plugin `carpeta` — los documentos que dejás en una carpeta entran a memex.

El caso que resuelve: extractos bancarios, facturas y contratos que descargás a mano. Los dejás en
una carpeta y el cliente local los recoge en su próxima pasada; el contenido lo lee el OCR (un PDF
digital se lee gratis, sin llave de visión) y de ahí sale lo que corresponda — en un extracto, las
transacciones de finanzas.

**Identidad por CONTENIDO**: el `external_id` es `carpeta:<sha256>`, así que volver a dejar el
mismo archivo NO lo duplica, y renombrarlo tampoco lo re-ingiere. Por eso el plugin no necesita
llevar registro de lo ya procesado ni mover tus archivos: la carpeta es tuya y queda como estaba.

El cursor guarda la fecha de modificación más alta ya vista, para no releer toda la carpeta en cada
pasada; se resta un margen porque un archivo copiado puede conservar una fecha vieja.

Config (`~/.memex-local-client/plugins/carpeta/config.toml`):

    folder = "C:/Users/yo/Documents/memex"   # obligatorio
    recursive = true                          # incluir subcarpetas (default true)
    max_mb = 10                               # ignora archivos más grandes (default 10)
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from memex.core.media_types import DOCUMENT_CONTENT_TYPES, IMAGE_CONTENT_TYPES, make_media_blob
from memex.core.payloads import BasePayload, DocumentPayload
from memex.core.source import HealthResult, Source, SourceKind, SourceRecord

name = "carpeta"
version = "0.1.0"
source_type = "carpeta"
default_schedule = "PT5M"

#: Extensión → content-type. Solo lo que memex sabe leer (`MEDIA_CONTENT_TYPES`): el resto se
#: ignora en silencio para que la carpeta pueda tener cualquier cosa sin ensuciar el inbox.
_BY_EXTENSION: dict[str, str] = {
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
#: Margen hacia atrás sobre el cursor: un archivo copiado conserva la fecha del original y podría
#: quedar por debajo de la marca. El dedup por sha256 hace inofensivo releer de más.
_MTIME_MARGIN = timedelta(hours=6)


def build_source(local_config: Mapping[str, Any]) -> Source:
    cfg = CarpetaConfig.model_validate(dict(local_config))
    return _CarpetaSource(cfg)


def validate_requirements(local_config: Mapping[str, Any]) -> list[Any]:
    from memex_local_client.protocol import Problem

    raw = dict(local_config)
    folder = str(raw.get("folder") or "").strip()
    if not folder:
        return [
            Problem(
                severity="error",
                code="folder_missing",
                message=(
                    'falta `folder`: agregá folder = "C:/ruta/a/tu/carpeta" en '
                    "~/.memex-local-client/plugins/carpeta/config.toml"
                ),
            )
        ]
    path = Path(folder).expanduser()
    if not path.is_dir():
        return [
            Problem(
                severity="error",
                code="folder_not_found",
                message=f"la carpeta {path} no existe: creala o corregí la ruta en su config.toml",
            )
        ]
    return []


class CarpetaConfig(BaseModel):
    folder: str
    recursive: bool = True
    max_mb: int = Field(default=10, ge=1)
    model_config = ConfigDict(extra="forbid")

    @property
    def path(self) -> Path:
        return Path(self.folder).expanduser()

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 1024 * 1024


class CarpetaCursor(BaseModel):
    """Fecha de modificación más alta ya vista (ISO). Solo evita releer la carpeta entera; la
    identidad real del documento es su sha256."""

    last_mtime: datetime | None = None
    model_config = ConfigDict(frozen=True, extra="forbid")


def _content_type(path: Path) -> str | None:
    ct = _BY_EXTENSION.get(path.suffix.lower())
    if ct is None:
        return None
    return ct if ct in (DOCUMENT_CONTENT_TYPES | IMAGE_CONTENT_TYPES) else None


class _CarpetaSource:
    """`Source[CarpetaCursor]` que emite un registro por archivo nuevo de la carpeta."""

    type: ClassVar[str] = "carpeta"
    kind: ClassVar[SourceKind] = SourceKind.DOCUMENTOS
    payload_schema: ClassVar[type[BasePayload]] = DocumentPayload
    config_schema: ClassVar[type[BaseModel]] = CarpetaConfig
    checkpoint_schema: ClassVar[type[BaseModel]] = CarpetaCursor

    def __init__(self, config: CarpetaConfig) -> None:
        self._config = config

    async def health_check(self) -> HealthResult:
        path = self._config.path
        ok = path.is_dir()
        return HealthResult(
            status="healthy" if ok else "unhealthy",
            detail=f"carpeta {path}" if ok else f"la carpeta {path} no existe",
            checked_at=datetime.now(UTC),
        )

    def _candidates(self, floor: datetime | None) -> list[Path]:
        root = self._config.path
        if not root.is_dir():
            return []
        it = root.rglob("*") if self._config.recursive else root.glob("*")
        out: list[Path] = []
        for p in it:
            if not p.is_file() or _content_type(p) is None:
                continue
            stat = p.stat()
            if stat.st_size > self._config.max_bytes or stat.st_size == 0:
                continue
            if floor is not None and datetime.fromtimestamp(stat.st_mtime, UTC) < floor:
                continue
            out.append(p)
        return sorted(out, key=lambda p: p.stat().st_mtime)

    def fetch(self, checkpoint: CarpetaCursor) -> Iterable[SourceRecord]:
        floor = (checkpoint.last_mtime - _MTIME_MARGIN) if checkpoint.last_mtime else None
        root = self._config.path
        for path in self._candidates(floor):
            content_type = _content_type(path)
            if content_type is None:  # ya filtrado en _candidates; defensivo
                continue
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            try:
                shown = str(path.relative_to(root).parent)
            except ValueError:  # pragma: no cover - path fuera de la raíz
                shown = ""
            payload = DocumentPayload(
                filename=path.name,
                content_type=content_type,
                size=len(data),
                sha256=sha,
                captured_from=f"carpeta {root.name}"
                + (f"/{shown}" if shown not in ("", ".") else ""),
                document_date=mtime,
            )
            yield SourceRecord(
                # Identidad por CONTENIDO: re-dejar el mismo archivo no duplica. Mismo prefijo y
                # misma clave que las otras vías de entrega (`memex.documentos.ingest`), para que
                # un documento que ya entró por el bot o por la web no entre otra vez por acá.
                external_id=f"documento:{sha}",
                occurred_at=mtime,
                payload=payload.model_dump(mode="json", by_alias=True),
                dedupe_keys=[f"sha256:{sha}"],
                media=[make_media_blob(data, content_type=content_type, filename=path.name)],
            )

    def advance_checkpoint(self, checkpoint: CarpetaCursor, last: SourceRecord) -> CarpetaCursor:
        previous = checkpoint.last_mtime
        current = last.occurred_at
        if previous is not None and previous > current:
            return checkpoint
        return CarpetaCursor(last_mtime=current)
