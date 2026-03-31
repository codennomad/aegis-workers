"""Prometheus metrics definitions for the Async Jobs System.

Metrics:
- jobs_total: Counter (task_name, status)
- job_duration_seconds: Histogram with custom buckets
- dlq_size: Gauge (current number of messages in DLQ)
- active_workers: Gauge (number of alive Celery workers)
"""
from __future__ import annotations

from typing import Final

import structlog
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

logger = structlog.get_logger(__name__)

# =============================================================================
# Metric definitions
# =============================================================================

# Counter: total jobs processed, by task name and final status
jobs_total: Final[Counter] = Counter(
    name="jobs_total",
    documentation="Total number of jobs processed, by task name and status",
    labelnames=["task_name", "status"],
)

# Allowed label values (security: prevent label cardinality explosion)
_ALLOWED_STATUSES: Final[frozenset[str]] = frozenset(
    {"success", "failed", "retry", "dlq_retry", "cancelled"}
)

# Canonical bucket boundaries — exported so tests can assert equality
DLQ_BUCKETS: Final[list[float]] = [0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]

# Histogram: job processing duration in seconds
job_duration_seconds: Final[Histogram] = Histogram(
    name="job_duration_seconds",
    documentation="Duration of job execution in seconds",
    labelnames=["task_name"],
    buckets=DLQ_BUCKETS,
)

# Gauge: current number of messages in the Dead Letter Queue
dlq_size: Final[Gauge] = Gauge(
    name="dlq_size",
    documentation="Current number of messages in the Dead Letter Queue",
)

# Gauge: number of active Celery workers
active_workers: Final[Gauge] = Gauge(
    name="active_workers",
    documentation="Number of active Celery workers responding to ping",
)


# =============================================================================
# Validated label helpers
# =============================================================================


def record_job_total(task_name: str, status: str) -> None:
    """
    Increment jobs_total counter with label validation.

    Raises ValueError for unknown status labels to prevent unbounded cardinality.
    """
    if status not in _ALLOWED_STATUSES:
        raise ValueError(
            f"Status '{status}' is not allowed. Valid statuses: {sorted(_ALLOWED_STATUSES)}"
        )
    jobs_total.labels(task_name=task_name, status=status).inc()


def record_job_duration(task_name: str, duration_seconds: float) -> None:
    """
    Observe job duration.

    Raises ValueError for negative durations.
    """
    if duration_seconds < 0:
        raise ValueError(
            f"Duration must be non-negative, got {duration_seconds} (negative values not allowed)"
        )
    job_duration_seconds.labels(task_name=task_name).observe(duration_seconds)


def update_dlq_size(count: int) -> None:
    """
    Update DLQ size gauge. Sets -1 as a sentinel for inspect errors.
    """
    dlq_size.set(float(count))


def update_active_workers_count(count: int) -> None:
    """Update active_workers gauge."""
    active_workers.set(float(count))


# =============================================================================
# Metrics endpoint response
# =============================================================================


def get_metrics_response() -> tuple[bytes, str]:
    """
    Generate Prometheus text format output for the /metrics endpoint.

    Returns:
        (content_bytes, content_type_string)
    """
    content = generate_latest(REGISTRY)
    return content, CONTENT_TYPE_LATEST
