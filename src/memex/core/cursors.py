"""Typed cursor (checkpoint) models per source type.

Each source defines its cursor shape as a Pydantic model and validates the
dict it reads/writes from memex against it. Catches malformed cursors at the
boundary instead of letting them propagate as KeyErrors mid-fetch.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FolderState(BaseModel):
    """Per-IMAP-folder checkpoint state."""

    uidvalidity: int
    last_uid: int

    model_config = ConfigDict(frozen=True, extra="forbid")


class ImapCursor(BaseModel):
    """IMAP checkpoint: per-folder UIDVALIDITY and last-seen UID.

    When UIDVALIDITY changes for a folder, the source resets `last_uid` to 0
    and emits a warning log so the operator notices the re-fetch.
    """

    folders: dict[str, FolderState] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
