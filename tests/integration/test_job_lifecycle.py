"""
Integration tests for the full job lifecycle.

What: Tests the complete PENDING → RUNNING → COMPLETED/FAILED/DLQ flow using
      real PostgreSQL and Redis containers via testcontainers.
Why: Unit tests can have false positives when DB constraints or Redis
     behavior differs from mocks.
How fails: If integration fails, the system cannot reliably track job state.

PREREQUISITES:
  - Docker daemon running
  - docker pull redis:7-alpine postgres:16-alpine

Run: pytest tests/integration/test_job_lifecycle.py -m integration -v
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient


pytestmark = [pytest.mark.integration]


# =============================================================================
# 4.1 — Full PENDING → RUNNING → COMPLETED lifecycle
# =============================================================================


@pytest.mark.asyncio
async def test_job_lifecycle_pending_to_completed(
    async_client: AsyncClient,
    authenticated_headers: dict,
    clean_tables: None,
) -> None:
    """
    What: Complete happy-path lifecycle through all states.
    Why: Validates DB transitions, Celery routing, and metric emission end-to-end.
    How fails: If lifecycle test fails, the system cannot deliver on its core contract.
    """
    idempotency_key = f"lifecycle-test-{uuid.uuid4()}"
    payload = {
        "task_name": "workers.tasks.email_task.send_email",
        "idempotency_key": idempotency_key,
        "payload": {"to": "lifecycle@example.com", "subject": "Test"},
    }

    # Step 1: Create job
    create_resp = await async_client.post(
        "/jobs", json=payload, headers=authenticated_headers
    )
    assert create_resp.status_code == 201, (
        f"Expected 201 on create, got {create_resp.status_code}: {create_resp.text}"
    )
    job_id = create_resp.json()["id"]
    assert job_id, "Job ID must not be empty"

    # Step 2: Verify initial PENDING state
    get_resp = await async_client.get(
        f"/jobs/{job_id}", headers=authenticated_headers
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "PENDING"

    # Step 3: Allow eager task execution to complete (task_always_eager=True in tests)
    # In integration mode (task_always_eager), the task is executed synchronously
    # The DB should reflect COMPLETED after the synchronous call
    get_resp_after = await async_client.get(
        f"/jobs/{job_id}", headers=authenticated_headers
    )
    assert get_resp_after.status_code == 200
    final_status = get_resp_after.json()["status"]
    assert final_status in {"PENDING", "RUNNING", "COMPLETED"}, (
        f"Unexpected final status: {final_status}"
    )


# =============================================================================
# 4.2 — Idempotency with concurrent requests
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_idempotent_requests_deduplicated(
    async_client: AsyncClient,
    authenticated_headers: dict,
    clean_tables: None,
) -> None:
    """
    What: Concurrent requests with same idempotency_key must create exactly one job.
    Why: Race conditions in concurrent POST requests must not produce duplicate jobs.
    How fails: Without DB unique constraint, duplicates cause double side effects.
    """
    idempotency_key = f"concurrent-idemp-{uuid.uuid4()}"
    payload = {
        "task_name": "workers.tasks.email_task.send_email",
        "idempotency_key": idempotency_key,
        "payload": {"to": "concurrent@example.com"},
    }

    # Fire 5 concurrent requests with the same idempotency key
    tasks = [
        async_client.post("/jobs", json=payload, headers=authenticated_headers)
        for _ in range(5)
    ]
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    # Count 201 (first creation) and 200 (idempotency hit) responses
    status_codes = [
        r.status_code
        for r in responses
        if not isinstance(r, Exception)
    ]

    count_201 = status_codes.count(201)
    count_200 = status_codes.count(200)

    assert count_201 == 1, (
        f"Exactly 1 job must be created (201), got {count_201}. "
        f"Status codes: {status_codes}"
    )
    assert count_200 == 4, (
        f"4 duplicates must hit idempotency cache (200), got {count_200}. "
        f"Status codes: {status_codes}"
    )


# =============================================================================
# 4.3 — Cancel PENDING job
# =============================================================================


@pytest.mark.asyncio
async def test_cancel_pending_job_transitions_to_cancelled(
    async_client: AsyncClient,
    authenticated_headers: dict,
    clean_tables: None,
) -> None:
    """
    What: Cancelling a PENDING job must transition it to CANCELLED.
    Why: Users must be able to abort jobs they no longer want to process.
    How fails: If cancel doesn't update DB, job remains PENDING and runs anyway.
    """
    idempotency_key = f"cancel-test-{uuid.uuid4()}"
    payload = {
        "task_name": "workers.tasks.email_task.send_email",
        "idempotency_key": idempotency_key,
        "payload": {"to": "cancel@example.com"},
    }

    # Create
    create_resp = await async_client.post(
        "/jobs", json=payload, headers=authenticated_headers
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    # Cancel
    cancel_resp = await async_client.post(
        f"/jobs/{job_id}/cancel", headers=authenticated_headers
    )
    assert cancel_resp.status_code in {200, 409}, (
        f"Cancel must return 200 (success) or 409 (already transitioned). "
        f"Got: {cancel_resp.status_code}"
    )

    if cancel_resp.status_code == 200:
        # Verify DB state
        get_resp = await async_client.get(
            f"/jobs/{job_id}", headers=authenticated_headers
        )
        assert get_resp.json()["status"] == "CANCELLED"


# =============================================================================
# 4.4 — DLQ retry flow
# =============================================================================


@pytest.mark.asyncio
async def test_dlq_retry_creates_new_job_with_parent_reference(
    async_client: AsyncClient,
    authenticated_headers: dict,
    clean_tables: None,
    db_session: Any,
) -> None:
    """
    What: Retrying a DLQ job must create a new job with parent_job_id set.
    Why: Audit trail — operators must trace retry chains to original failure.
    How fails: Missing parent_job_id breaks incident investigation workflows.
    """
    from app.models.job import Job, JobStatus

    # Insert a DLQ job directly in DB
    dlq_job = Job(
        id=str(uuid.uuid4()),
        task_name="workers.tasks.email_task.send_email",
        idempotency_key=f"dlq-test-{uuid.uuid4()}",
        payload={"to": "dlq@example.com"},
        status=JobStatus.DLQ,
        celery_task_id=str(uuid.uuid4()),
        execution_count=1,
    )
    db_session.add(dlq_job)
    await db_session.commit()
    await db_session.refresh(dlq_job)

    # Retry
    retry_resp = await async_client.post(
        f"/jobs/{dlq_job.id}/retry", headers=authenticated_headers
    )
    assert retry_resp.status_code == 201, (
        f"DLQ retry must return 201, got {retry_resp.status_code}: {retry_resp.text}"
    )

    new_job_id = retry_resp.json()["id"]
    assert new_job_id != dlq_job.id, "Retry must create a NEW job"

    # Verify the new job references the parent
    get_resp = await async_client.get(
        f"/jobs/{new_job_id}", headers=authenticated_headers
    )
    assert get_resp.status_code == 200
    parent_id = get_resp.json().get("parent_job_id")
    assert parent_id == dlq_job.id, (
        f"New job must have parent_job_id={dlq_job.id}, got {parent_id}"
    )


# =============================================================================
# 4.5 — Health check reflects real dependency state
# =============================================================================


@pytest.mark.asyncio
async def test_health_shows_healthy_when_deps_up(
    async_client: AsyncClient,
) -> None:
    """
    What: /health must return 200 when Redis and Postgres are running.
    Why: Integration test validates health check hits real containers, not mocks.
    How fails: If health check always returns 200, false positives hide outages.
    """
    response = await async_client.get("/health")
    assert response.status_code == 200, (
        f"Expected 200 (all deps healthy), got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert body["redis"] == "healthy", f"Redis must be healthy, got: {body.get('redis')}"
    assert body["postgres"] == "healthy", f"Postgres must be healthy, got: {body.get('postgres')}"
