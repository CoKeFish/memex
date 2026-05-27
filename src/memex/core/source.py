from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class SourceRecord:
    """Lo que produce un Source. El runner añade user_id y source_id al insertar."""

    external_id: str
    occurred_at: datetime
    payload: dict[str, Any]
    dedupe_keys: list[str]


class Source(Protocol):
    type: str

    def fetch(self, checkpoint: dict[str, Any] | None) -> Iterable[SourceRecord]: ...

    def advance_checkpoint(
        self, checkpoint: dict[str, Any] | None, last: SourceRecord
    ) -> dict[str, Any]: ...
