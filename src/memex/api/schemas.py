from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    source_id: int
    external_id: str
    occurred_at: datetime
    payload: dict[str, Any]
    dedupe_keys: list[str] = Field(default_factory=list)


class IngestResponse(BaseModel):
    inserted: bool | None = None
    id: int | None = None
    reason: str | None = None
    would_insert: bool | None = None
    validations: dict[str, Any] | None = None


class IngestBatchRequest(BaseModel):
    records: list[IngestRequest]


class IngestBatchResponse(BaseModel):
    inserted: int
    duplicates: int
    errors: int


class InboxRow(BaseModel):
    id: int
    source_id: int
    external_id: str
    occurred_at: datetime
    received_at: datetime
    payload: dict[str, Any]
    processed_at: datetime | None
    process_error: str | None
    attempts: int


class InboxList(BaseModel):
    items: list[InboxRow]
    next_cursor: int | None = None


class StatsBySource(BaseModel):
    total: int
    pending: int
    errored: int


class InboxStats(BaseModel):
    sources: dict[int, StatsBySource]


class SourceCreate(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class SourceRow(BaseModel):
    id: int
    user_id: int
    name: str
    type: str
    enabled: bool
    config: dict[str, Any]
    created_at: datetime


class CheckpointBody(BaseModel):
    cursor: dict[str, Any]
