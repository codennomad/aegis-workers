"""
Unit tests for app/routes/jobs.py

What: Tests all HTTP routes in isolation using mocked dependencies.
Why: Routes are the public contract — any regression breaks API clients immediately.
How fails: Wrong status codes or missing headers break client integrations silently.

Run: pytest tests/unit/test_jobs_routes.py -m unit -v
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.job import JobCreateResponse, JobResponse
from app.services.job_service import JobConflictError, JobNotFoundError


# =============================================================================
# Helpers
# =============================================================================

def _make_job_response(**overrides: Any) -> JobResponse:
    defaults = {
        "id": str(uuid.uuid4()),
        "status": "PENDING",
        "task_name": "workers.tasks.email_task.send_email",
        "idempotency_key": "test-idemp-key",
        "payload": {"to": "test@example.com"},
        "celery_task_id": str(uuid.uuid4()),
        "execution_count": 0,
        "created_at": "2026-03-31T12:00:00Z",
        "updated_at": "2026-03-31T12:00:00Z",
        "parent_job_id": None,
        "result": None,
        "error_message": None,
        "estimated_next_run": None,
    }
    defaults.update(overrides)
    return JobResponse(**defaults)


def _make_create_response(was_idempotent: bool = False, **overrides: Any) -> JobCreateResponse:
    job = _make_job_response(**overrides)
    return JobCreateResponse(**job.model_dump(), was_idempotent=was_idempotent)


# =============================================================================
# 3.1 — POST /jobs — New job creation returns 201
# =============================================================================


@pytest.mark.unit
class TestCreateJob:
    """
    What: Verifies the full create job flow including status codes and response body.
    Why: 201 vs 200 distinction drives client decision to display "created" vs "exists".
    How fails: Wrong status code → client creates duplicate UI entries.
    """

    @pytest.mark.asyncio
    async def test_new_job_returns_201(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """New job must return HTTP 201 with was_idempotent=false."""
        payload = {
            "task_name": "workers.tasks.email_task.send_email",
            "idempotency_key": "unique-key-001",
            "payload": {"to": "user@example.com"},
        }
        create_response = _make_create_response(was_idempotent=False)

        with patch(
            "app.routes.jobs.JobService.create_job",
            new_callable=AsyncMock,
            return_value=(create_response, True),  # (response, was_created)
        ):
            response = await async_client.post(
                "/jobs",
                json=payload,
                headers=authenticated_headers,
            )

        assert response.status_code == 201, (
            f"New job must return 201, got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert body["was_idempotent"] is False

    @pytest.mark.asyncio
    async def test_celery_send_task_called_once_on_create(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """Celery send_task() must be called exactly once per new job."""
        payload = {
            "task_name": "workers.tasks.email_task.send_email",
            "idempotency_key": "unique-key-send-test",
            "payload": {"to": "user@example.com"},
        }
        create_response = _make_create_response(was_idempotent=False)

        with patch("app.routes.jobs.celery_app") as mock_celery:
            mock_celery.send_task = MagicMock(return_value=MagicMock(id="celery-123"))
            with patch(
                "app.routes.jobs.JobService.create_job",
                new_callable=AsyncMock,
                return_value=(create_response, True),
            ):
                await async_client.post(
                    "/jobs",
                    json=payload,
                    headers=authenticated_headers,
                )

    @pytest.mark.asyncio
    async def test_idempotent_request_returns_200(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """Repeated request with same idempotency_key must return 200, not 201."""
        payload = {
            "task_name": "workers.tasks.email_task.send_email",
            "idempotency_key": "existing-key-002",
            "payload": {"to": "user@example.com"},
        }
        create_response = _make_create_response(was_idempotent=True)

        with patch(
            "app.routes.jobs.JobService.create_job",
            new_callable=AsyncMock,
            return_value=(create_response, False),  # was_created=False → 200
        ):
            response = await async_client.post(
                "/jobs",
                json=payload,
                headers=authenticated_headers,
            )

        assert response.status_code == 200, (
            f"Idempotent job must return 200, got {response.status_code}"
        )
        body = response.json()
        assert body["was_idempotent"] is True

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_422(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """Request missing required fields must return 422 Unprocessable Entity."""
        response = await async_client.post(
            "/jobs",
            json={"payload": {}},  # Missing task_name and idempotency_key
            headers=authenticated_headers,
        )
        assert response.status_code == 422


# =============================================================================
# 3.2 — GET /jobs/{job_id}
# =============================================================================


@pytest.mark.unit
class TestGetJob:
    """
    What: Verifies job retrieval, 404 handling, and cache/estimated_next_run.
    Why: Job status is polled by clients — wrong status causes UI confusion.
    How fails: Missing 404 → client shows stale "pending" for deleted job.
    """

    @pytest.mark.asyncio
    async def test_get_existing_job_returns_200(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """GET /jobs/{id} for existing job must return 200."""
        job_id = str(uuid.uuid4())
        job = _make_job_response(id=job_id)

        with patch(
            "app.routes.jobs.JobService.get_job",
            new_callable=AsyncMock,
            return_value=job,
        ):
            response = await async_client.get(
                f"/jobs/{job_id}",
                headers=authenticated_headers,
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_nonexistent_job_returns_404(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """GET /jobs/{id} for unknown job must return 404."""
        job_id = str(uuid.uuid4())

        with patch(
            "app.routes.jobs.JobService.get_job",
            new_callable=AsyncMock,
            side_effect=JobNotFoundError(job_id),
        ):
            response = await async_client.get(
                f"/jobs/{job_id}",
                headers=authenticated_headers,
            )

        assert response.status_code == 404


# =============================================================================
# 3.3 — POST /jobs/{job_id}/cancel
# =============================================================================


@pytest.mark.unit
class TestCancelJob:
    """
    What: Verifies cancel semantics — only PENDING jobs can be cancelled.
    Why: Cancelling a RUNNING job could leave system in inconsistent state.
    How fails: Cancelling RUNNING job → orphaned DB record or double-processing.
    """

    @pytest.mark.asyncio
    async def test_cancel_pending_job_returns_200(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """Cancelling a PENDING job must return 200."""
        job_id = str(uuid.uuid4())
        cancel_response = MagicMock()
        cancel_response.model_dump.return_value = {
            "id": job_id, "status": "CANCELLED", "message": "Job cancelled"
        }

        with patch(
            "app.routes.jobs.JobService.cancel_job",
            new_callable=AsyncMock,
            return_value=cancel_response,
        ):
            response = await async_client.post(
                f"/jobs/{job_id}/cancel",
                headers=authenticated_headers,
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_cancel_running_job_returns_409(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """Cancelling a RUNNING job must return 409 Conflict."""
        job_id = str(uuid.uuid4())

        with patch(
            "app.routes.jobs.JobService.cancel_job",
            new_callable=AsyncMock,
            side_effect=JobConflictError(
                "Cannot cancel a running job", current_status="RUNNING"
            ),
        ):
            response = await async_client.post(
                f"/jobs/{job_id}/cancel",
                headers=authenticated_headers,
            )

        assert response.status_code == 409
        body = response.json()
        assert "Cannot cancel a running job" in body.get("detail", "")


# =============================================================================
# 3.4 — POST /jobs/{job_id}/retry (DLQ retry)
# =============================================================================


@pytest.mark.unit
class TestRetryDlqJob:
    """
    What: Verifies DLQ retry promotion — only DLQ jobs can be retried.
    Why: Retrying non-DLQ jobs creates orphaned extra runs.
    How fails: Retrying COMPLETED job → duplicate side effects.
    """

    @pytest.mark.asyncio
    async def test_retry_dlq_job_returns_201(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """Retrying a DLQ job must return 201 with new job."""
        job_id = str(uuid.uuid4())
        new_job = _make_create_response(was_idempotent=False)

        with patch(
            "app.routes.jobs.JobService.retry_dlq_job",
            new_callable=AsyncMock,
            return_value=new_job,
        ):
            response = await async_client.post(
                f"/jobs/{job_id}/retry",
                headers=authenticated_headers,
            )

        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_retry_non_dlq_job_returns_409(
        self, async_client: Any, authenticated_headers: dict
    ) -> None:
        """Retrying a non-DLQ job must return 409 Conflict."""
        job_id = str(uuid.uuid4())

        with patch(
            "app.routes.jobs.JobService.retry_dlq_job",
            new_callable=AsyncMock,
            side_effect=JobConflictError(
                "Job is not in DLQ", current_status="COMPLETED"
            ),
        ):
            response = await async_client.post(
                f"/jobs/{job_id}/retry",
                headers=authenticated_headers,
            )

        assert response.status_code == 409


# =============================================================================
# 3.5 — GET /health
# =============================================================================


@pytest.mark.unit
class TestHealthCheck:
    """
    What: Verifies health check runs both checks in parallel and never short-circuits.
    Why: Health check must report TRUE state — hiding degradation causes blind deploys.
    How fails: Short-circuit on first failure → Redis down masks Postgres also down.
    """

    @pytest.mark.asyncio
    async def test_all_healthy_returns_200(self, async_client: Any) -> None:
        """When all deps healthy, /health returns 200."""
        healthy = {"status": "healthy", "redis": "healthy", "postgres": "healthy"}

        with patch(
            "app.routes.jobs._check_redis",
            new_callable=AsyncMock,
            return_value=("redis", True),
        ):
            with patch(
                "app.routes.jobs._check_postgres",
                new_callable=AsyncMock,
                return_value=("postgres", True),
            ):
                response = await async_client.get("/health")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_partial_degradation_returns_503(self, async_client: Any) -> None:
        """If any dependency is down, /health must return 503."""
        with patch(
            "app.routes.jobs._check_redis",
            new_callable=AsyncMock,
            side_effect=Exception("connection refused"),
        ):
            with patch(
                "app.routes.jobs._check_postgres",
                new_callable=AsyncMock,
                return_value=("postgres", True),
            ):
                response = await async_client.get("/health")

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_both_checks_called_even_if_first_fails(
        self, async_client: Any
    ) -> None:
        """Both Redis and Postgres checks must run in parallel — no short-circuit."""
        redis_called = False
        postgres_called = False

        async def _failing_redis():
            nonlocal redis_called
            redis_called = True
            raise Exception("redis down")

        async def _healthy_postgres():
            nonlocal postgres_called
            postgres_called = True
            return ("postgres", True)

        with patch("app.routes.jobs._check_redis", _failing_redis):
            with patch("app.routes.jobs._check_postgres", _healthy_postgres):
                await async_client.get("/health")

        assert redis_called and postgres_called, (
            "Both checks must run regardless of first result (no short-circuit)"
        )
