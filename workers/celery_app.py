"""Celery application configuration — production-grade setup.

Design decisions:
- Redis as broker AND result backend (simplicity; Redis Streams as upgrade path)
- Late ack (acks_late=True): message returned to queue on worker crash
- Prefetch=1: prevents one worker from hoarding tasks
- DLQ as a separate named queue: isolation + independent monitoring
- JSON serializer: avoids pickle security vulnerabilities (OWASP A08)
- Result TTL: 24h (configurable via CELERY_TASK_RESULT_EXPIRES)
"""
from __future__ import annotations

from celery import Celery
from celery.signals import worker_ready, worker_shutdown
from kombu import Exchange, Queue

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

settings = get_settings()

# =============================================================================
# Queue definitions
# =============================================================================

default_exchange = Exchange("default", type="direct")
dlq_exchange = Exchange("dlq", type="direct")

DEFAULT_QUEUE = Queue(
    settings.default_queue_name,
    exchange=default_exchange,
    routing_key=settings.default_queue_name,
)
DLQ_QUEUE = Queue(
    settings.dlq_queue_name,
    exchange=dlq_exchange,
    routing_key=settings.dlq_queue_name,
    queue_arguments={
        # Messages in DLQ are kept for 7 days for inspection
        "x-message-ttl": 7 * 24 * 60 * 60 * 1000,  # ms
    },
)

# =============================================================================
# Celery app factory
# =============================================================================

celery_app = Celery(
    "async_jobs",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["workers.tasks.email_task"],
)

celery_app.conf.update(
    # -------------------------------------------------------------------------
    # Serialization (security: no pickle)
    # -------------------------------------------------------------------------
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # -------------------------------------------------------------------------
    # Reliability
    # -------------------------------------------------------------------------
    task_acks_late=True,          # late ack: msg requeued on worker crash
    task_reject_on_worker_lost=True,  # explicit NACK on SIGKILL
    worker_prefetch_multiplier=1,  # 1 task per worker at a time
    # -------------------------------------------------------------------------
    # Result backend
    # -------------------------------------------------------------------------
    result_expires=settings.celery_task_result_expires,  # 24h
    result_backend_transport_options={
        "retry_policy": {
            "timeout": 5.0,
        }
    },
    # -------------------------------------------------------------------------
    # Timezone
    # -------------------------------------------------------------------------
    enable_utc=True,
    timezone="UTC",
    # -------------------------------------------------------------------------
    # Queues
    # -------------------------------------------------------------------------
    task_default_queue=settings.default_queue_name,
    task_default_exchange=settings.default_queue_name,
    task_default_routing_key=settings.default_queue_name,
    task_queues=[DEFAULT_QUEUE, DLQ_QUEUE],
    task_routes={
        "workers.tasks.dlq.*": {"queue": settings.dlq_queue_name},
    },
    # -------------------------------------------------------------------------
    # Beat schedule (example: periodic DLQ size check)
    # -------------------------------------------------------------------------
    beat_schedule={
        "update-dlq-size-gauge": {
            "task": "workers.tasks.email_task.update_dlq_metrics",
            "schedule": 30.0,  # every 30 seconds
        },
    },
    # -------------------------------------------------------------------------
    # Worker
    # -------------------------------------------------------------------------
    worker_max_tasks_per_child=1000,  # recycle workers to prevent memory leaks
    worker_disable_rate_limits=False,
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    # -------------------------------------------------------------------------
    # Broker connection resilience
    # -------------------------------------------------------------------------
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,
    broker_transport_options={
        "retry_policy": {
            "timeout": 5.0,
            "max_retries": 3,
            "interval_start": 0.2,
            "interval_step": 0.2,
            "interval_max": 1.0,
        }
    },
)


# =============================================================================
# Worker lifecycle signals
# =============================================================================


@worker_ready.connect
def on_worker_ready(**kwargs: object) -> None:  # type: ignore[misc]
    """Log when worker becomes ready to process tasks."""
    logger.info("celery_worker_ready", worker=str(kwargs.get("sender", "unknown")))


@worker_shutdown.connect
def on_worker_shutdown(**kwargs: object) -> None:  # type: ignore[misc]
    """Log graceful worker shutdown."""
    logger.info("celery_worker_shutdown", worker=str(kwargs.get("sender", "unknown")))
