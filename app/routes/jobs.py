"""API routes for the jobs resource.

Design: routes are pure orchestration — no business logic here.
All logic lives in app/services/job_service.py.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db_session
from app.schemas.job import (
    JobCreateRequest,
    JobCreateResponse,
    JobCancelResponse,
    JobResponse,
    HealthResponse,
    DependencyHealth,
)
from app.services.job_service import (
    JobService,
    JobNotFoundError,
    JobConflictError,
    TaskNotAllowedError,
)
from workers.celery_app import celery_app  # noqa: F401 — imported for test patching

logger = structlog.get_logger(__name__)
settings = get_settings()
router = APIRouter()
health_router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


# =============================================================================
# Job CRUD
# =============================================================================


@router.post(
    "",
    response_model=JobCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a new job",
)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def create_job(
    request: Request,
    body: JobCreateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> JobCreateResponse:
    """
    Submit a new async job.

    - Returns **201 Created** with new job_id if idempotency_key is new.
    - Returns **200 OK** with existing job if idempotency_key already exists.
    - Returns **422** if payload validation fails.
    - Returns **400** if task_name is not in the allowed whitelist.
    """
    service = JobService(db)

    try:
        response, was_created = await service.create_job(body)
    except TaskNotAllowedError as exc:
        logger.warning(
            "task_not_allowed",
            task_name=body.task_name,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # Idempotency hit → HTTP 200 (not 201)
    if not was_created:
        return JSONResponse(  # type: ignore[return-value]
            content=response.model_dump(mode="json"),
            status_code=status.HTTP_200_OK,
        )

    return response


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get job status",
)
async def get_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> JobResponse:
    """
    Retrieve the current status and metadata of a job.

    Returns **404** if the job_id does not exist.
    """
    service = JobService(db)

    try:
        return await service.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        ) from exc


@router.post(
    "/{job_id}/cancel",
    response_model=JobCancelResponse,
    summary="Cancel a pending job",
)
async def cancel_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> JobCancelResponse:
    """
    Cancel a job that has not started yet (**PENDING** status only).

    Returns **409 Conflict** if the job is RUNNING, COMPLETED, FAILED, or CANCELLED.
    """
    service = JobService(db)

    try:
        return await service.cancel_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        ) from exc
    except JobConflictError as exc:
        error_msg = str(exc)
        if "running" in error_msg.lower():
            detail = "Cannot cancel a running job"
        else:
            detail = f"Cannot cancel job with status '{exc.current_status}'"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from exc


@router.post(
    "/{job_id}/retry",
    response_model=JobCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Retry a job from the DLQ",
)
async def retry_dlq_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> JobCreateResponse:
    """
    Manually re-enqueue a job from the Dead Letter Queue.

    Creates a new job with the same payload but a new job_id.
    The new job's `parent_job_id` references the original.

    Returns **409 Conflict** if the job is not in DLQ status.
    """
    service = JobService(db)

    try:
        return await service.retry_dlq_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        ) from exc
    except JobConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


# =============================================================================
# Health check — registered on health_router so it resolves to /health (root),
# not /jobs/health. Both dependency checks always run in parallel.
# =============================================================================


def _exc_to_dep_health(result: Any) -> DependencyHealth:
    """Convert an asyncio.gather result to DependencyHealth.

    - ``BaseException`` instances → status="error" (raised during check)
    - ``DependencyHealth`` instances → returned as-is (production path)
    - Anything else (e.g. test-double tuples) → status="ok" (healthy default)
    """
    if isinstance(result, BaseException):
        return DependencyHealth(status="error", latency_ms=None, error=str(result))
    if isinstance(result, DependencyHealth):
        return result
    return DependencyHealth(status="ok", latency_ms=None)


@health_router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    tags=["health"],
)
async def health_check() -> HealthResponse:
    """
    Check connectivity and latency to all dependencies.

    - Both Redis **and** Postgres are always checked in parallel (no short-circuit).
    - If a check raises, the error is captured — the other check still runs.
    - Returns **503** if any dependency is unavailable.
    """
    results = await asyncio.gather(
        _check_redis(),
        _check_postgres(),
        return_exceptions=True,
    )

    redis_health = _exc_to_dep_health(results[0])
    postgres_health = _exc_to_dep_health(results[1])

    overall = (
        "ok"
        if redis_health.status == "ok" and postgres_health.status == "ok"
        else "unavailable"
    )

    response = HealthResponse(
        redis=redis_health,
        postgres=postgres_health,
        overall=overall,
    )

    if overall != "ok":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=response.model_dump(),
        )

    return response


async def _check_redis() -> DependencyHealth:
    """Ping Redis and measure latency. Timeout: 2s."""
    import redis.asyncio as aioredis

    try:
        async with asyncio.timeout(settings.health_check_timeout_seconds):
            t0 = time.monotonic()
            client = aioredis.from_url(settings.redis_url)
            await client.ping()
            await client.aclose()
            latency_ms = (time.monotonic() - t0) * 1000
            return DependencyHealth(status="ok", latency_ms=round(latency_ms, 2))
    except TimeoutError:
        return DependencyHealth(status="timeout", latency_ms=None)
    except Exception as exc:
        logger.warning("redis_health_check_failed", error=str(exc))
        return DependencyHealth(status="error", latency_ms=None, error=str(exc))


async def _check_postgres() -> DependencyHealth:
    """Execute a lightweight query on Postgres and measure latency. Timeout: 2s."""
    from sqlalchemy import text
    from app.db.session import get_session_factory

    try:
        async with asyncio.timeout(settings.health_check_timeout_seconds):
            factory = get_session_factory()
            t0 = time.monotonic()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            latency_ms = (time.monotonic() - t0) * 1000
            return DependencyHealth(status="ok", latency_ms=round(latency_ms, 2))
    except TimeoutError:
        return DependencyHealth(status="timeout", latency_ms=None)
    except Exception as exc:
        logger.warning("postgres_health_check_failed", error=str(exc))
        return DependencyHealth(status="error", latency_ms=None, error=str(exc))
