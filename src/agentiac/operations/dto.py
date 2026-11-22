"""Typed job DTOs: no filesystem paths, credentials, raw command text or trace output."""
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


class JobRequestDTO(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=128)
    args: dict = Field(default_factory=dict)
    timeout_s: float = Field(default=30.0, ge=1, le=30)


class JobAcceptedDTO(BaseModel):
    job_id: str
    status: str
    location: str
    as_of: str


class JobDTO(BaseModel):
    id: str
    idempotency_key: str
    actor: str
    operation: str
    target: str
    args_hash: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    attempt: int = 0
    lease_until: Optional[str] = None
    timeout_s: float
    cancelability: str
    result: Optional[dict] = None
    reason: Optional[str] = None


class JobListDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    next_cursor: Optional[str] = None
    jobs: list[JobDTO]


class EventDTO(BaseModel):
    event_id: int
    at: str
    kind: str
    message: str


class EventsDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    next_cursor: Optional[str] = None
    events: list[EventDTO]
    reason: Optional[str] = None


class LogChunkDTO(BaseModel):
    schema_version: int = 1
    as_of: str
    next_cursor: Optional[str] = None
    lines: list[str] = Field(default_factory=list)
    reason: Optional[str] = None
