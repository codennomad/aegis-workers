"""Pydantic v2 schemas for Job API request/response."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.job import JobStatus


# =============================================================================
# Request schemas
# =============================================================================


class JobCreateRequest(BaseModel):
    """
    Schema for POST /jobs.

    Security: extra='forbid' prevents mass assignment (OWASP A03).
    The task_name is validated against the whitelist in the service layer.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Fully qualified task name (validated against whitelist)",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Task-specific payload data",
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Unique key to prevent duplicate job submissions",
    )

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, v: str) -> str:
        """
        Reject idempotency keys with characters that could cause log injection
        or path traversal (OWASP A03, A09).
        """
        forbidden_chars = set('\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\r\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f')
        if any(c in forbidden_chars for c in v):
            raise ValueError("idempotency_key contains invalid control characters")
        return v

    @field_validator("payload")
    @classmethod
    def validate_payload_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Reject payloads that are excessively large (DoS prevention)."""
        import json
        serialized = json.dumps(v)
        if len(serialized) > 1_000_000:  # 1MB limit
            raise ValueError("payload exceeds maximum size of 1MB")
        return v


# =============================================================================
# Response schemas
# =============================================================================


class JobResponse(BaseModel):
    """
    Full job representation returned by GET /jobs/{id}.

    Field names follow the public API contract validated in suite_test.md.
    ORM name mapping: error_detail → error_message (public name).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_name: str
    idempotency_key: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    celery_task_id: str | None = None
    execution_count: int = 0
    created_at: datetime
    updated_at: datetime
    parent_job_id: uuid.UUID | None = None
    result: Any = None
    error_message: str | None = None  # maps from ORM's error_detail
    estimated_next_run: datetime | None = None  # computed by service layer


class JobCreateResponse(JobResponse):
    """
    Response for POST /jobs — extends JobResponse with HTTP semantics hint.

    Inherits all job fields so clients can act immediately without a second GET.
    `was_idempotent=True` signals HTTP 200 (existing) vs HTTP 201 (new).
    """

    was_idempotent: bool = False


class JobCancelResponse(BaseModel):
    """Response for POST /jobs/{id}/cancel."""

    id: uuid.UUID
    status: str
    message: str


class JobRetryResponse(BaseModel):
    """Response for POST /jobs/{id}/retry — returns new job."""

    new_job_id: uuid.UUID
    original_job_id: uuid.UUID
    status: str
    message: str


# =============================================================================
# Health check schemas
# =============================================================================


class DependencyHealth(BaseModel):
    """Health status of a single dependency."""

    status: str  # "ok" | "error" | "timeout"
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    """Response for GET /health."""

    redis: DependencyHealth
    postgres: DependencyHealth
    # Overall status derived from dependencies — NOT short-circuited
    overall: str  # "ok" | "degraded" | "unavailable"
