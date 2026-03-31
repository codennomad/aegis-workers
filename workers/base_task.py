"""Base Celery task with production-grade reliability patterns.

Features:
- Exponential backoff retry: 2 ** retries * 10 seconds
- Idempotency via Redis cache (idempotency_key)
- on_failure: move to DLQ + update DB status + emit metric
- on_success: update DB status + emit metric + record duration
- Structured logging with job_id, trace_id, worker_id
- Pydantic v2 input validation (ValidationError → DLQ, no retry)
"""
from __future__ import annotations

import time
import traceback
import uuid
from typing import Any

import structlog
from celery import Task
from celery.exceptions import MaxRetriesExceededError
from opentelemetry import trace
from pydantic import ValidationError

from app.config import get_settings
from app.metrics import jobs_total, job_duration_seconds, dlq_size

logger = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)
settings = get_settings()


class BaseTask(Task):
    """
    Base Celery task that every application task must inherit from.

    Provides:
    - Automatic retry with exponential backoff
    - Idempotency via Redis
    - Database status updates (via sync DB calls using asyncio.run)
    - Prometheus metric emission
    - Structured JSON logging
    """

    abstract = True
    autoretry_for = (Exception,)
    max_retries = 5
    default_retry_delay = 10
    # Do NOT autoretry ValidationError — it is a programming bug, goes to DLQ
    dont_autoretry_for = (ValidationError,)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _get_job_id(self) -> str:
        """Extract job_id from task kwargs, falling back to celery task id."""
        return str(
            self.request.kwargs.get("job_id", self.request.id or "unknown")
        )

    def _get_trace_id(self) -> str:
        """Extract OpenTelemetry trace id from current span context."""
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
        return "no-trace"

    def _get_worker_id(self) -> str:
        """Return the current Celery worker hostname."""
        return str(self.request.hostname or "unknown-worker")

    def _get_redis_client(self) -> Any:
        """Return a Redis client for idempotency checks."""
        import redis as redis_lib
        return redis_lib.from_url(settings.redis_url, decode_responses=True)

    def _check_idempotency(self, idempotency_key: str) -> tuple[bool, Any]:
        """
        Check if this idempotency_key was already processed.

        Returns:
            (True, cached_result) if already processed
            (False, None) if not yet processed
        """
        client = self._get_redis_client()
        cache_key = f"idempotency:{idempotency_key}"
        cached = client.get(cache_key)
        if cached is not None:
            import json
            result = json.loads(cached) if cached != "null" else None
            logger.info(
                "idempotency_hit",
                idempotency_key=idempotency_key,
                job_id=idempotency_key,
            )
            return True, result
        return False, None

    def _store_idempotency_result(self, idempotency_key: str, result: Any) -> None:
        """Store execution result in Redis for idempotency check."""
        import json
        client = self._get_redis_client()
        cache_key = f"idempotency:{idempotency_key}"
        # Store for same duration as task results
        client.setex(
            cache_key,
            settings.celery_task_result_expires,
            json.dumps(result),
        )

    def _update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        worker_id: str | None = None,
        error_detail: str | None = None,
        result: Any = None,
        increment_execution: bool = False,
    ) -> None:
        """
        Update job status in the database synchronously.

        Uses asyncio.run() because Celery workers run in sync context.
        Error is caught and logged — DB failure must not mask task status.
        """
        import asyncio
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from sqlalchemy import update, text
        from app.models.job import Job
        from datetime import datetime, timezone

        async def _do_update() -> None:
            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
                values: dict[str, Any] = {
                    "status": status,
                    "updated_at": datetime.now(timezone.utc),
                }
                if worker_id is not None:
                    values["worker_id"] = worker_id
                if error_detail is not None:
                    values["error_detail"] = error_detail[:4096]  # truncate
                if result is not None:
                    values["result"] = result
                if status == "RUNNING":
                    values["started_at"] = datetime.now(timezone.utc)
                if status in ("COMPLETED", "FAILED", "DLQ"):
                    values["completed_at"] = datetime.now(timezone.utc)
                if increment_execution:
                    # Uses server-side increment to avoid race condition
                    await session.execute(
                        update(Job)
                        .where(Job.id == uuid.UUID(job_id))
                        .values(**values)
                        .values(execution_count=Job.execution_count + 1)
                    )
                else:
                    await session.execute(
                        update(Job)
                        .where(Job.id == uuid.UUID(job_id))
                        .values(**values)
                    )
                await session.commit()
            await engine.dispose()

        try:
            asyncio.run(_do_update())
        except Exception as exc:
            logger.error(
                "db_status_update_failed",
                job_id=job_id,
                target_status=status,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _send_to_dlq(
        self,
        job_id: str,
        task_name: str,
        payload: dict[str, Any],
        error_detail: str,
    ) -> str:
        """
        Forward the task to the DLQ with full context.

        Returns the DLQ message ID for tracking.
        """
        from workers.celery_app import celery_app

        dlq_result = celery_app.send_task(
            "dlq.receive",
            kwargs={
                "original_job_id": job_id,
                "original_task_name": task_name,
                "payload": payload,
                "error_detail": error_detail,
            },
            queue=settings.dlq_queue_name,
        )
        return str(dlq_result.id)

    # -------------------------------------------------------------------------
    # Lifecycle hooks
    # -------------------------------------------------------------------------

    def before_start(self, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        """Mark job as RUNNING before execution begins."""
        job_id = str(kwargs.get("job_id", task_id))
        structlog.contextvars.bind_contextvars(
            job_id=job_id,
            trace_id=self._get_trace_id(),
            worker_id=self._get_worker_id(),
            task_name=self.name,
        )
        self._update_job_status(
            job_id,
            "RUNNING",
            worker_id=self._get_worker_id(),
            increment_execution=True,
        )
        logger.info("task_started", task_id=task_id, task_name=self.name)

    def on_success(
        self,
        retval: Any,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Called after successful task execution."""
        job_id = str(kwargs.get("job_id", task_id))
        task_name = self.name or "unknown"

        self._update_job_status(job_id, "COMPLETED", result=retval)

        # Emit Prometheus metrics
        jobs_total.labels(task_name=task_name, status="success").inc()

        # Record duration if start time is available
        start_time = getattr(self.request, "_start_time", None)
        if start_time is not None:
            duration = time.monotonic() - start_time
            job_duration_seconds.labels(task_name=task_name).observe(duration)

        # Store idempotency result
        idempotency_key = str(kwargs.get("idempotency_key", job_id))
        self._store_idempotency_result(idempotency_key, retval)

        logger.info(
            "task_succeeded",
            task_id=task_id,
            task_name=task_name,
            job_id=job_id,
        )

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: object,
    ) -> None:
        """Called after all retries are exhausted or on non-retryable error."""
        job_id = str(kwargs.get("job_id", task_id))
        task_name = self.name or "unknown"
        error_detail = str(exc)
        full_traceback = traceback.format_exc()

        # Move to DLQ
        try:
            payload = kwargs.get("payload", {})
            dlq_msg_id = self._send_to_dlq(
                job_id=job_id,
                task_name=task_name,
                payload=payload if isinstance(payload, dict) else {},
                error_detail=error_detail,
            )
        except Exception as dlq_exc:
            dlq_msg_id = "dlq-send-failed"
            logger.error(
                "dlq_send_failed",
                job_id=job_id,
                error=str(dlq_exc),
            )

        # Update DB to FAILED
        self._update_job_status(
            job_id,
            "FAILED",
            error_detail=f"{error_detail}\n\n{full_traceback}"[:4096],
        )

        # Update DLQ message ID and status
        self._update_job_status(job_id, "DLQ")

        # Emit Prometheus metric
        jobs_total.labels(task_name=task_name, status="failed").inc()

        # Update DLQ size gauge
        try:
            dlq_size.inc()
        except Exception:
            pass

        logger.error(
            "task_failed_terminal",
            task_id=task_id,
            task_name=task_name,
            job_id=job_id,
            trace_id=self._get_trace_id(),
            error_type=type(exc).__name__,
            error_detail=error_detail,
            stack_trace=full_traceback,
            dlq_msg_id=dlq_msg_id,
        )

    def on_retry(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: object,
    ) -> None:
        """Called before each retry attempt."""
        job_id = str(kwargs.get("job_id", task_id))
        retry_num = self.request.retries
        countdown = 2 ** retry_num * 10

        # Emit metric for retry
        jobs_total.labels(task_name=self.name or "unknown", status="retry").inc()

        logger.warning(
            "task_retrying",
            task_id=task_id,
            task_name=self.name,
            job_id=job_id,
            retry_number=retry_num,
            countdown_seconds=countdown,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    # -------------------------------------------------------------------------
    # run() — override in subclasses
    # -------------------------------------------------------------------------

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """
        Execute the task.

        Subclasses must override this method.
        The idempotency check and retry logic wrap the actual execution.
        """
        # Record start time for duration tracking
        self.request._start_time = time.monotonic()  # type: ignore[attr-defined]

        job_id = str(kwargs.get("job_id", self.request.id))
        idempotency_key = str(kwargs.get("idempotency_key", job_id))
        task_name = self.name or "unknown"
        retry_num = self.request.retries

        # Bind logging context
        structlog.contextvars.bind_contextvars(
            job_id=job_id,
            trace_id=self._get_trace_id(),
            worker_id=self._get_worker_id(),
            task_name=task_name,
        )

        # Idempotency check
        already_processed, cached_result = self._check_idempotency(idempotency_key)
        if already_processed:
            return cached_result

        # Compute retry countdown: 2 ** retries * 10 (10s, 20s, 40s, 80s, 160s)
        countdown = 2 ** retry_num * 10

        try:
            result = self.execute(*args, **kwargs)
            return result
        except ValidationError as exc:
            # Validation errors are not retried — go straight to DLQ
            logger.error(
                "task_validation_error",
                job_id=job_id,
                error=str(exc),
                error_type="ValidationError",
            )
            self.on_failure(exc, self.request.id or "", (), kwargs, None)
            raise
        except Exception as exc:
            if retry_num >= self.max_retries:
                # Max retries exceeded — on_failure will be called by Celery
                raise
            raise self.retry(exc=exc, countdown=countdown) from exc

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        """
        Override this method in subclasses with the actual task logic.

        The run() method handles idempotency, retry, and error handling.
        """
        raise NotImplementedError(
            f"Task {self.__class__.__name__} must implement execute()"
        )
