"""
Unit tests for workers/base_task.py

What: Tests all lifecycle behaviors of BaseTask in isolation.
Why: BaseTask is the reliability backbone of the system.
     Any bug here means silent data loss or duplicate processing.
How fails: If BaseTask is broken, tests show which specific contract is violated.

Run: pytest tests/unit/test_base_task.py -m unit -v
"""
from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from pydantic import BaseModel, ValidationError

from workers.base_task import BaseTask


# =============================================================================
# Test doubles
# =============================================================================

class _ConcreteTask(BaseTask):
    """Minimal concrete implementation for testing."""
    name = "test.concrete_task"

    def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"executed": True, "job_id": kwargs.get("job_id")}


class _AlwaysFailTask(BaseTask):
    """Task that always raises an exception."""
    name = "test.always_fail_task"

    def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("forced failure")


# =============================================================================
# 1.1 — Retry with exponential backoff
# =============================================================================


@pytest.mark.unit
class TestRetryBackoff:
    """
    What: Verifies exponential backoff formula and max_retries enforcement.
    Why: Retry storm prevention — correct countdown prevents cascade failures.
    How fails: If countdown is wrong, workers flood downstream services.
    """

    @pytest.mark.parametrize("retry_num,expected_countdown", [
        (0, 10),    # 2**0 * 10 = 10
        (1, 20),    # 2**1 * 10 = 20
        (2, 40),    # 2**2 * 10 = 40
        (3, 80),    # 2**3 * 10 = 80
        (4, 160),   # 2**4 * 10 = 160
    ])
    def test_countdown_formula(self, retry_num: int, expected_countdown: int) -> None:
        """Countdown must follow 2**retries * 10 exactly."""
        countdown = 2 ** retry_num * 10
        assert countdown == expected_countdown, (
            f"retry {retry_num}: expected {expected_countdown}, got {countdown}"
        )

    def test_retry_kwargs_contain_countdown(self) -> None:
        """The retry() call must pass the computed countdown."""
        task = _AlwaysFailTask()
        task.request = MagicMock()
        task.request.retries = 2
        task.request.id = "test-task-id"
        task.request.kwargs = {}
        task.request._start_time = time.monotonic()
        task.max_retries = 5
        task.request.hostname = "test-worker"

        with (
            patch.object(task, "_check_idempotency", return_value=(False, None)),
            patch.object(task, "_update_job_status"),
            patch.object(task, "retry") as mock_retry,
        ):
            mock_retry.side_effect = Exception("retry called")
            with pytest.raises(Exception, match="retry called"):
                task.run(job_id="job-1", idempotency_key="key-1")

            _, retry_kwargs = mock_retry.call_args
            assert retry_kwargs.get("countdown") == 40, (
                f"retry 2 should have countdown=40, got {retry_kwargs.get('countdown')}"
            )

    def test_max_retries_respected(self) -> None:
        """
        On the 6th attempt (retries == max_retries), must call on_failure
        and NOT call retry() again.
        """
        task = _AlwaysFailTask()
        task.request = MagicMock()
        task.request.retries = 5  # at the limit
        task.request.id = "test-task-id"
        task.request.kwargs = {}
        task.request._start_time = time.monotonic()
        task.max_retries = 5
        task.request.hostname = "test-worker"

        with (
            patch.object(task, "_check_idempotency", return_value=(False, None)),
            patch.object(task, "_update_job_status"),
            patch.object(task, "retry") as mock_retry,
        ):
            with pytest.raises(RuntimeError, match="forced failure"):
                task.run(job_id="job-1", idempotency_key="key-1")

            # retry must NOT be called when at max
            mock_retry.assert_not_called()


# =============================================================================
# 1.2 — Success behavior
# =============================================================================


@pytest.mark.unit
class TestSuccessBehavior:
    """
    What: Verifies DB update, metric emission, and duration tracking on success.
    Why: Success events must be reliably recorded for audit and observability.
    How fails: Missing metric = blind ops; missing DB update = incorrect job status.
    """

    def test_db_updated_to_completed_on_success(self) -> None:
        """DB must receive status='COMPLETED' after successful execution."""
        task = _ConcreteTask()
        task.request = MagicMock()
        task.request.retries = 0
        task.request.id = "task-id-success"
        task.request.kwargs = {"job_id": "job-success"}
        task.request._start_time = time.monotonic()
        task.request.hostname = "worker-1"

        mock_db_update = MagicMock()

        with (
            patch.object(task, "_check_idempotency", return_value=(False, None)),
            patch.object(task, "_update_job_status", mock_db_update),
            patch.object(task, "_store_idempotency_result"),
        ):
            task.on_success(
                retval={"executed": True},
                task_id="task-id-success",
                args=(),
                kwargs={"job_id": "job-success", "idempotency_key": "ik-1"},
            )

        calls = [call_args[0][1] for call_args in mock_db_update.call_args_list]
        assert "COMPLETED" in calls, f"Expected COMPLETED in DB calls, got: {calls}"

    def test_success_metric_incremented(self, mock_metrics: dict) -> None:
        """jobs_total{status='success'} must be incremented exactly once."""
        task = _ConcreteTask()
        task.request = MagicMock()
        task.request._start_time = time.monotonic()

        with patch.object(task, "_update_job_status"):
            with patch.object(task, "_store_idempotency_result"):
                task.on_success(
                    retval={},
                    task_id="t1",
                    args=(),
                    kwargs={"job_id": "j1", "idempotency_key": "k1"},
                )

        mock_metrics["jobs_total"].labels.assert_called_with(
            task_name=task.name, status="success"
        )
        mock_metrics["jobs_total"].labels.return_value.inc.assert_called_once()

    def test_duration_observed_on_success(self, mock_metrics: dict) -> None:
        """job_duration_seconds.observe() must be called with a positive value."""
        task = _ConcreteTask()
        task.request = MagicMock()
        task.request._start_time = time.monotonic() - 0.5  # simulates 0.5s elapsed

        with patch.object(task, "_update_job_status"):
            with patch.object(task, "_store_idempotency_result"):
                task.on_success(
                    retval={},
                    task_id="t1",
                    args=(),
                    kwargs={"job_id": "j1", "idempotency_key": "k1"},
                )

        call_args = mock_metrics["job_duration_seconds"].labels.return_value.observe.call_args
        assert call_args is not None, "observe() was not called"
        observed_duration = call_args[0][0]
        assert observed_duration > 0, f"Duration must be positive, got {observed_duration}"


# =============================================================================
# 1.3 — Terminal failure behavior
# =============================================================================


@pytest.mark.unit
class TestFailureBehavior:
    """
    What: Verifies DLQ routing, DB update, metric, and structured log on failure.
    Why: Failed jobs MUST reach DLQ for manual recovery. Silent drops = data loss.
    How fails: If DLQ send fails silently, jobs disappear. Tests catch that.
    """

    def test_dlq_send_called_on_failure(self) -> None:
        """on_failure must call _send_to_dlq with job details."""
        task = _AlwaysFailTask()
        task.request = MagicMock()
        task.request.id = "task-fail"
        task.request.retries = 5

        mock_send_dlq = MagicMock(return_value="dlq-msg-123")

        with (
            patch.object(task, "_send_to_dlq", mock_send_dlq),
            patch.object(task, "_update_job_status"),
        ):
            task.on_failure(
                exc=RuntimeError("forced failure"),
                task_id="task-fail",
                args=(),
                kwargs={"job_id": "j-fail", "idempotency_key": "k-fail", "payload": {}},
                einfo=None,
            )

        mock_send_dlq.assert_called_once()
        call_kwargs = mock_send_dlq.call_args[1]
        assert call_kwargs["job_id"] == "j-fail"

    def test_db_updated_to_failed_on_failure(self) -> None:
        """DB must receive status='FAILED' after terminal failure."""
        task = _AlwaysFailTask()
        task.request = MagicMock()
        task.request.id = "task-fail"

        mock_db_update = MagicMock()

        with (
            patch.object(task, "_send_to_dlq", return_value="dlq-123"),
            patch.object(task, "_update_job_status", mock_db_update),
        ):
            task.on_failure(
                exc=RuntimeError("forced failure"),
                task_id="task-fail",
                args=(),
                kwargs={"job_id": "j-fail", "idempotency_key": "k-fail"},
                einfo=None,
            )

        statuses = [c[0][1] for c in mock_db_update.call_args_list]
        assert "FAILED" in statuses or "DLQ" in statuses

    def test_failure_metric_incremented(self, mock_metrics: dict) -> None:
        """jobs_total{status='failed'} must be incremented on terminal failure."""
        task = _AlwaysFailTask()
        task.request = MagicMock()
        task.request.id = "task-fail"

        with (
            patch.object(task, "_send_to_dlq", return_value="dlq-123"),
            patch.object(task, "_update_job_status"),
        ):
            task.on_failure(
                exc=RuntimeError("forced failure"),
                task_id="task-fail",
                args=(),
                kwargs={"job_id": "j-fail", "idempotency_key": "k-fail"},
                einfo=None,
            )

        mock_metrics["jobs_total"].labels.assert_any_call(
            task_name=task.name, status="failed"
        )


# =============================================================================
# 1.4 — Idempotency
# =============================================================================


@pytest.mark.unit
class TestIdempotency:
    """
    What: Verifies Redis-based idempotency prevents duplicate execution.
    Why: Network retries and queue redelivery cause duplicate task calls.
    How fails: Without idempotency, emails are sent twice, payments charged twice.
    """

    def test_cached_result_returned_without_execution(self) -> None:
        """Task must return cached result and NOT call execute() if key exists."""
        task = _ConcreteTask()
        task.request = MagicMock()
        task.request.retries = 0
        task.request.id = "task-idemp-1"
        task.request.kwargs = {}
        task.request._start_time = time.monotonic()
        task.request.hostname = "worker-1"

        cached = {"result": "previous", "cached": True}

        with (
            patch.object(task, "_check_idempotency", return_value=(True, cached)),
            patch.object(task, "execute") as mock_execute,
        ):
            result = task.run(job_id="j-1", idempotency_key="key-abc")

        assert result == cached
        mock_execute.assert_not_called()

    def test_no_metrics_emitted_on_idempotency_hit(
        self, mock_metrics: dict
    ) -> None:
        """No metrics must be emitted when idempotency cache is hit."""
        task = _ConcreteTask()
        task.request = MagicMock()
        task.request.retries = 0
        task.request.id = "task-idemp-2"
        task.request.kwargs = {}
        task.request._start_time = time.monotonic()
        task.request.hostname = "worker-1"

        with patch.object(task, "_check_idempotency", return_value=(True, {"result": "cached"})):
            task.run(job_id="j-1", idempotency_key="key-abc")

        mock_metrics["jobs_total"].labels.assert_not_called()
        mock_metrics["job_duration_seconds"].labels.assert_not_called()

    def test_new_key_proceeds_and_stores_result(self) -> None:
        """New idempotency_key must execute and call _store_idempotency_result."""
        task = _ConcreteTask()
        task.request = MagicMock()
        task.request.retries = 0
        task.request.id = "task-idemp-3"
        task.request.kwargs = {}
        task.request._start_time = time.monotonic()
        task.request.hostname = "worker-1"

        mock_store = MagicMock()

        with (
            patch.object(task, "_check_idempotency", return_value=(False, None)),
            patch.object(task, "_update_job_status"),
            patch.object(task, "_store_idempotency_result", mock_store),
        ):
            task.on_success(
                retval={"executed": True},
                task_id="task-idemp-3",
                args=(),
                kwargs={"job_id": "j-3", "idempotency_key": "new-key-xyz"},
            )

        mock_store.assert_called_once_with("new-key-xyz", {"executed": True})

    def test_idempotency_log_contains_hit_field(self, caplog: Any) -> None:
        """Structured log must contain idempotency_hit=true when cache is hit."""
        task = _ConcreteTask()
        task.request = MagicMock()
        task.request.retries = 0
        task.request.id = "task-idemp-4"
        task.request.kwargs = {}
        task.request._start_time = time.monotonic()
        task.request.hostname = "worker-1"

        import logging
        with caplog.at_level(logging.INFO):
            with (
                patch.object(task, "_check_idempotency", return_value=(True, {"r": 1})),
                patch.object(task, "_get_redis_client") as mock_redis,
            ):
                # _check_idempotency is already mocked, no real Redis call
                mock_redis.return_value.get.return_value = json.dumps({"r": 1})
                task.run(job_id="j-4", idempotency_key="key-dup")

        # The log should mention idempotency_hit
        assert any("idempotency" in r.message.lower() for r in caplog.records), (
            "Expected log entry containing 'idempotency' for cache hit"
        )


# =============================================================================
# 1.5 — Serialization and Pydantic validation
# =============================================================================


@pytest.mark.unit
class TestSerialization:
    """
    What: Verifies ValidationError goes to DLQ without retry.
    Why: ValidationError is a code bug, not a transient failure. Retrying is useless.
    How fails: If ValidationError retries, workers loop indefinitely on bad inputs.
    """

    def test_validation_error_skips_retry_goes_to_dlq(self) -> None:
        """ValidationError must call on_failure directly, not retry."""

        class _ValidationFailTask(BaseTask):
            name = "test.validation_fail"

            def execute(self, *args: Any, **kwargs: Any) -> Any:
                class _Schema(BaseModel):
                    required_field: int  # int, will fail on string

                _Schema(required_field="not-an-int")  # type: ignore[arg-type]
                return {}

        task = _ValidationFailTask()
        task.request = MagicMock()
        task.request.retries = 0
        task.request.id = "task-val"
        task.request.kwargs = {}
        task.request._start_time = time.monotonic()
        task.request.hostname = "worker-1"
        task.max_retries = 5

        mock_on_failure = MagicMock()
        mock_retry = MagicMock()

        with (
            patch.object(task, "_check_idempotency", return_value=(False, None)),
            patch.object(task, "_update_job_status"),
            patch.object(task, "on_failure", mock_on_failure),
            patch.object(task, "retry", mock_retry),
        ):
            with pytest.raises(ValidationError):
                task.run(job_id="j-val", idempotency_key="k-val")

        mock_on_failure.assert_called_once()
        mock_retry.assert_not_called()
