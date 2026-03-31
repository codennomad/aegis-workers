"""Job service layer — all business logic lives here, not in routes."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.metrics import jobs_total, record_job_total
from app.models.job import Job, JobStatus
from app.schemas.job import (
    JobCreateRequest,
    JobCreateResponse,
    JobResponse,
    JobCancelResponse,
)

logger = structlog.get_logger(__name__)
settings = get_settings()


class JobNotFoundError(Exception):
    """Raised when a job with the given ID does not exist."""


class JobConflictError(Exception):
    """Raised when an operation is not allowed for the current job status."""

    def __init__(self, message: str, current_status: str) -> None:
        super().__init__(message)
        self.current_status = current_status


class TaskNotAllowedError(Exception):
    """Raised when a task_name is not in the allowed whitelist."""


class JobService:
    """
    Encapsulates all business logic for the jobs domain.

    Responsibilities:
    - Validate task_name against whitelist (prevent task injection)
    - Check and enforce idempotency
    - Persist job state transitions
    - Enqueue / revoke / re-enqueue Celery tasks
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # -------------------------------------------------------------------------
    # Create
    # -------------------------------------------------------------------------

    async def create_job(self, req: JobCreateRequest) -> tuple[JobCreateResponse, bool]:
        """
        Submit a new job.

        Returns:
            (response, was_created) — was_created=False means idempotency hit.

        Raises:
            TaskNotAllowedError: if task_name not in whitelist.
        """
        # Security: whitelist validation (prevents Celery task injection, OWASP A03)
        if req.task_name not in settings.allowed_task_names:
            raise TaskNotAllowedError(
                f"Task '{req.task_name}' is not in the allowed task whitelist"
            )

        # Check idempotency
        existing = await self._find_by_idempotency_key(req.idempotency_key)
        if existing is not None:
            logger.info(
                "job_idempotency_hit",
                idempotency_key=req.idempotency_key,
                existing_job_id=str(existing.id),
            )
            return (
                JobCreateResponse(
                    id=existing.id,
                    task_name=existing.task_name,
                    idempotency_key=existing.idempotency_key,
                    status=existing.status,
                    payload=existing.payload or {},
                    celery_task_id=existing.celery_task_id,
                    execution_count=existing.execution_count,
                    parent_job_id=existing.parent_job_id,
                    result=existing.result,
                    error_message=existing.error_detail,
                    created_at=existing.created_at,
                    updated_at=existing.updated_at,
                    was_idempotent=True,
                ),
                False,
            )

        # Create new job record
        job = Job(
            id=uuid.uuid4(),
            task_name=req.task_name,
            payload=req.payload,
            idempotency_key=req.idempotency_key,
            status=JobStatus.PENDING.value,
        )
        self._db.add(job)
        await self._db.flush()

        # Enqueue task to Celery
        from workers.celery_app import celery_app
        celery_result = celery_app.send_task(
            req.task_name,
            kwargs={
                "job_id": str(job.id),
                "idempotency_key": req.idempotency_key,
                "payload": req.payload,
            },
            queue=settings.default_queue_name,
        )

        # Store Celery task ID
        job.celery_task_id = celery_result.id
        await self._db.commit()
        await self._db.refresh(job)

        logger.info(
            "job_created",
            job_id=str(job.id),
            task_name=req.task_name,
            celery_task_id=celery_result.id,
        )

        return (
            JobCreateResponse(
                id=job.id,
                task_name=job.task_name,
                idempotency_key=job.idempotency_key,
                status=job.status,
                payload=job.payload or {},
                celery_task_id=job.celery_task_id,
                execution_count=job.execution_count,
                parent_job_id=None,
                result=None,
                error_message=None,
                created_at=job.created_at,
                updated_at=job.updated_at,
                was_idempotent=False,
            ),
            True,
        )

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    async def get_job(self, job_id: uuid.UUID) -> JobResponse:
        """
        Return job details, including estimated_next_run for PENDING/RUNNING jobs.

        Raises:
            JobNotFoundError: if job does not exist.
        """
        job = await self._find_by_id(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")

        estimated_next_run: datetime | None = None
        if job.status == JobStatus.PENDING.value and job.retry_count > 0:
            countdown = 2 ** job.retry_count * 10
            estimated_next_run = datetime.now(timezone.utc) + timedelta(seconds=countdown)

        return JobResponse(
            id=job.id,
            task_name=job.task_name,
            idempotency_key=job.idempotency_key,
            status=job.status,
            payload=job.payload or {},
            celery_task_id=job.celery_task_id,
            execution_count=job.execution_count,
            parent_job_id=job.parent_job_id,
            result=job.result,
            error_message=job.error_detail,
            created_at=job.created_at,
            updated_at=job.updated_at,
            estimated_next_run=estimated_next_run,
        )

    # -------------------------------------------------------------------------
    # Cancel
    # -------------------------------------------------------------------------

    async def cancel_job(self, job_id: uuid.UUID) -> JobCancelResponse:
        """
        Cancel a PENDING job by revoking it from Celery and updating DB.

        Raises:
            JobNotFoundError: if job does not exist.
            JobConflictError: if job is not in PENDING status.
        """
        job = await self._find_by_id(job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")

        if job.status != JobStatus.PENDING.value:
            raise JobConflictError(
                f"Cannot cancel job in status '{job.status}'. Only PENDING jobs can be cancelled.",
                current_status=job.status,
            )

        # Revoke from Celery (do not terminate running workers)
        if job.celery_task_id:
            from workers.celery_app import celery_app
            celery_app.control.revoke(job.celery_task_id, terminate=False)

        # Update DB
        await self._db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.CANCELLED.value,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self._db.commit()

        logger.info("job_cancelled", job_id=str(job_id))

        return JobCancelResponse(
            id=job_id,
            status=JobStatus.CANCELLED.value,
            message="Job successfully cancelled",
        )

    # -------------------------------------------------------------------------
    # DLQ Retry
    # -------------------------------------------------------------------------

    async def retry_dlq_job(self, job_id: uuid.UUID) -> JobCreateResponse:
        """
        Re-enqueue a job from the DLQ as a new job with same payload.

        The new job references the original via parent_job_id.

        Raises:
            JobNotFoundError: if job does not exist.
            JobConflictError: if job is not in DLQ status.
        """
        original = await self._find_by_id(job_id)
        if original is None:
            raise JobNotFoundError(f"Job {job_id} not found")

        if original.status != JobStatus.DLQ.value:
            raise JobConflictError(
                f"Job is not in DLQ (current status: '{original.status}'). "
                "Only DLQ jobs can be retried via this endpoint.",
                current_status=original.status,
            )

        # Create a new job with same payload, new ID, new idempotency key
        new_idempotency_key = f"dlq-retry-{uuid.uuid4()}"
        new_job = Job(
            id=uuid.uuid4(),
            task_name=original.task_name,
            payload=original.payload,
            idempotency_key=new_idempotency_key,
            status=JobStatus.PENDING.value,
            parent_job_id=original.id,
            retry_count=original.retry_count + 1,
        )
        self._db.add(new_job)
        await self._db.flush()

        from workers.celery_app import celery_app
        celery_result = celery_app.send_task(
            original.task_name,
            kwargs={
                "job_id": str(new_job.id),
                "idempotency_key": new_idempotency_key,
                "payload": original.payload,
            },
            queue=settings.default_queue_name,
        )

        new_job.celery_task_id = celery_result.id
        await self._db.commit()

        # Emit DLQ retry metric
        try:
            record_job_total(task_name=original.task_name, status="dlq_retry")
        except Exception:
            pass

        logger.info(
            "job_dlq_retry",
            original_job_id=str(job_id),
            new_job_id=str(new_job.id),
        )

        await self._db.refresh(new_job)
        return JobCreateResponse(
            id=new_job.id,
            task_name=new_job.task_name,
            idempotency_key=new_job.idempotency_key,
            status=new_job.status,
            payload=new_job.payload or {},
            celery_task_id=new_job.celery_task_id,
            execution_count=new_job.execution_count,
            parent_job_id=new_job.parent_job_id,
            result=None,
            error_message=None,
            created_at=new_job.created_at,
            updated_at=new_job.updated_at,
            was_idempotent=False,
        )

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    async def _find_by_id(self, job_id: uuid.UUID) -> Job | None:
        result = await self._db.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()

    async def _find_by_idempotency_key(self, key: str) -> Job | None:
        result = await self._db.execute(
            select(Job).where(Job.idempotency_key == key)
        )
        return result.scalar_one_or_none()
