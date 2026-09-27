"""Plugin `selftest` — emite correos sintéticos para probar el caño punta a punta.

No lee nada del sistema: genera N `EmailPayload` deterministas y los empuja por el
gateway. Sirve para confirmar, sin datos reales, que `connect → /state → /ingest`
funciona y que los records caen en el inbox. Los `external_id` son estables, así que
re-correrlo deduplica (no duplica) — buen smoke de idempotencia.

`source_type = "outlook"`: tipo EMAIL ya registrado y **push-only** (sin fetch a demanda),
para no exponer botones de "traer" sobre una fuente de juguete. Borralo cuando termines:
`memex-local-client plugin uninstall selftest` (y la source `selftest` desde /carga).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from memex.core.payloads import Address, BasePayload, EmailPayload, msgid_dedupe_key
from memex.core.source import HealthResult, Source, SourceKind, SourceRecord

name = "selftest"
version = "0.1.0"
source_type = "outlook"
default_schedule = "PT5M"


def build_source(local_config: Mapping[str, Any]) -> Source:
    return _SelftestSource(count=int(local_config.get("count", 2)))


def validate_requirements(local_config: Mapping[str, Any]) -> list:
    # Sin requisitos externos — siempre listo.
    return []


class SelftestConfig(BaseModel):
    count: int = 2
    model_config = ConfigDict(extra="forbid")


class SelftestCursor(BaseModel):
    emitted: int = 0
    model_config = ConfigDict(frozen=True, extra="forbid")


class _SelftestSource:
    """`Source[SelftestCursor]` que emite correos sintéticos deterministas."""

    count: int

    type: ClassVar[str] = "outlook"
    kind: ClassVar[SourceKind] = SourceKind.EMAIL
    payload_schema: ClassVar[type[BasePayload]] = EmailPayload
    config_schema: ClassVar[type[BaseModel]] = SelftestConfig
    checkpoint_schema: ClassVar[type[BaseModel]] = SelftestCursor

    def __init__(self, count: int = 2) -> None:
        self.count = max(1, count)

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="selftest siempre ok", checked_at=datetime.now(UTC)
        )

    def fetch(self, checkpoint: SelftestCursor) -> Iterable[SourceRecord]:
        now = datetime.now(UTC)
        for i in range(1, self.count + 1):
            msg_id = f"selftest-{i}@memex.local"
            payload = EmailPayload(
                from_=Address(email="selftest@memex.local", name="memex selftest"),
                to=[Address(email="me@local")],
                subject=f"memex selftest #{i} — el caño funciona",
                date=now,
                message_id=msg_id,
                body_text=(
                    "Mensaje de prueba generado por el plugin selftest del cliente local. "
                    "Si lo ves en el inbox, la conexión y la ingesta andan."
                ),
                folder="selftest",
            )
            key = msgid_dedupe_key(msg_id)
            yield SourceRecord(
                external_id=f"selftest:{i}",
                occurred_at=now,
                payload=payload.model_dump(mode="json", by_alias=True),
                dedupe_keys=[key] if key else [],
            )

    def advance_checkpoint(self, checkpoint: SelftestCursor, last: SourceRecord) -> SelftestCursor:
        return SelftestCursor(emitted=checkpoint.emitted + 1)
