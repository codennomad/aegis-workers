"""Example application tasks — email sending and DLQ utilities."""
from __future__ import annotations

from typing import Any

import structlog

from workers.celery_app import celery_app
from workers.base_task import BaseTask
from app.metrics import dlq_size, active_workers

logger = structlog.get_logger(__name__)


@celery_app.task(
    bind=True,
    base=BaseTask,
    name="workers.tasks.email_task.send_email",
    max_retries=5,
)
def send_email(self: BaseTask, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """
    Send a transactional email.

    Kwargs:
        job_id: UUID of the job record
        idempotency_key: unique key to prevent duplicate sends
        payload: dict with 'to', 'subject', 'body'
    """
    return self.run(*args, **kwargs)


@celery_app.task(
    bind=True,
    base=BaseTask,
    name="workers.tasks.email_task.send_bulk_email",
    max_retries=5,
)
def send_bulk_email(self: BaseTask, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """
    Send bulk emails in a single task.

    Kwargs:
        job_id: UUID of the job record
        idempotency_key: unique key
        payload: dict with 'recipients' list, 'subject', 'body'
    """
    return self.run(*args, **kwargs)


@celery_app.task(
    name="dlq.receive",
    bind=True,
    max_retries=0,  # DLQ tasks never auto-retry
    acks_late=True,
)
def dlq_receive(
    self: Any, **kwargs: Any
) -> dict[str, Any]:
    """
    Receive a message into the DLQ.

    This task does NOT process — it only stores for manual inspection/retry.
    """
    logger.error(
        "dlq_message_received",
        original_job_id=kwargs.get("original_job_id"),
        original_task_name=kwargs.get("original_task_name"),
        error_detail=kwargs.get("error_detail"),
    )
    return {
        "dlq_received": True,
        "original_job_id": kwargs.get("original_job_id"),
    }


@celery_app.task(name="workers.tasks.email_task.update_dlq_metrics")
def update_dlq_metrics() -> None:
    """
    Periodic task (run by celery-beat every 30s) that updates DLQ and worker gauges.
    """
    from app.config import get_settings
    import redis as redis_lib

    cfg = get_settings()
    client = redis_lib.from_url(cfg.redis_url)

    try:
        dlq_length = client.llen(cfg.dlq_queue_name)
        dlq_size.set(float(dlq_length))
    except Exception as exc:
        logger.error("dlq_size_check_failed", error=str(exc))
        dlq_size.set(-1.0)

    try:
        from workers.celery_app import celery_app as app
        inspect = app.control.inspect(timeout=2.0)
        active = inspect.active()
        count = len(active) if active else 0
        active_workers.set(float(count))
    except Exception as exc:
        logger.error("active_workers_check_failed", error=str(exc))
        active_workers.set(0.0)
