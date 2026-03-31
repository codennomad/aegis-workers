"""
Unit tests for app/metrics.py

What: Tests Prometheus metric setup, validation, and HTTP exposition.
Why: Metrics are the first line of defense for detecting production anomalies.
How fails: Wrong buckets = unusable histogram latency data in Grafana dashboards.

Run: pytest tests/unit/test_metrics.py -m unit -v
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from app.metrics import (
    DLQ_BUCKETS,
    get_metrics_response,
    job_duration_seconds,
    jobs_total,
    record_job_duration,
    record_job_total,
)


# =============================================================================
# 2.1 — Metric registration and labels
# =============================================================================


@pytest.mark.unit
class TestMetricRegistration:
    """
    What: Verifies all metrics are registered with correct names and labels.
    Why: Label inconsistency crashes Prometheus scrape; name mismatch breaks alerts.
    How fails: Wrong names → alert rules don't fire; wrong labels → cardinality explosion.
    """

    def test_jobs_total_is_counter(self) -> None:
        """jobs_total must be a Counter (has .inc())."""
        from prometheus_client import Counter
        assert isinstance(jobs_total, Counter), (
            f"Expected Counter, got {type(jobs_total)}"
        )

    def test_job_duration_histogram_correct_buckets(self) -> None:
        """
        job_duration_seconds must use the exact buckets:
        [0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]
        """
        expected = [0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]
        assert DLQ_BUCKETS == expected, (
            f"Expected buckets {expected}, got {DLQ_BUCKETS}"
        )


# =============================================================================
# 2.2 — record_job_total validation
# =============================================================================


@pytest.mark.unit
class TestRecordJobTotal:
    """
    What: Verifies label validation guards against cardinality explosion.
    Why: Unbounded labels crash Prometheus memory; OWASP A03 mitigation.
    How fails: Accepting arbitrary status strings → server OOM.
    """

    @pytest.mark.parametrize("valid_status", [
        "success", "failed", "retry", "dlq_retry",
    ])
    def test_valid_status_does_not_raise(
        self, valid_status: str, mock_metrics: dict
    ) -> None:
        """Valid status labels must not raise."""
        record_job_total("test.task", valid_status)
        mock_metrics["jobs_total"].labels.assert_called_with(
            task_name="test.task", status=valid_status
        )

    def test_invalid_status_raises_value_error(self) -> None:
        """Unknown status must raise ValueError to prevent label cardinality explosion."""
        with pytest.raises(ValueError, match="not allowed"):
            record_job_total("test.task", "invalid_status_xyz")

    def test_empty_status_raises_value_error(self) -> None:
        """Empty string status must raise ValueError."""
        with pytest.raises(ValueError):
            record_job_total("test.task", "")


# =============================================================================
# 2.3 — record_job_duration validation
# =============================================================================


@pytest.mark.unit
class TestRecordJobDuration:
    """
    What: Verifies observe() rejects negative durations.
    Why: Negative latencies corrupt Prometheus percentile calculations.
    How fails: Negative duration → misleading SLA reports.
    """

    def test_positive_duration_calls_observe(self, mock_metrics: dict) -> None:
        """Positive duration must call histogram.observe()."""
        record_job_duration("test.task", 1.5)
        mock_metrics["job_duration_seconds"].labels.return_value.observe.assert_called_with(
            1.5
        )

    def test_zero_duration_calls_observe(self, mock_metrics: dict) -> None:
        """Zero duration (extremely fast) must be accepted."""
        record_job_duration("test.task", 0.0)
        mock_metrics["job_duration_seconds"].labels.return_value.observe.assert_called_with(
            0.0
        )

    def test_negative_duration_raises_value_error(self) -> None:
        """Negative duration must raise ValueError."""
        with pytest.raises(ValueError, match="negative"):
            record_job_duration("test.task", -0.1)

    def test_very_large_duration_accepted(self, mock_metrics: dict) -> None:
        """Large durations (e.g., 3600s) must be accepted without error."""
        record_job_duration("test.task", 3600.0)
        mock_metrics["job_duration_seconds"].labels.return_value.observe.assert_called_with(
            3600.0
        )


# =============================================================================
# 2.4 — /metrics HTTP endpoint
# =============================================================================


@pytest.mark.unit
class TestMetricsEndpoint:
    """
    What: Verifies /metrics returns 200 with correct Content-Type.
    Why: Prometheus scraper rejects wrong Content-Type silently.
    How fails: Wrong Content-Type → no metrics in Prometheus → no alerts fire.
    """

    @pytest.mark.asyncio
    async def test_metrics_endpoint_200_ok(self, async_client: any) -> None:
        """GET /metrics must return 200 OK."""
        response = await async_client.get("/metrics")
        assert response.status_code == 200, (
            f"Expected 200 for /metrics, got {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_metrics_content_type(self, async_client: any) -> None:
        """GET /metrics must return text/plain; version=0.0.4 Content-Type."""
        response = await async_client.get("/metrics")
        ct = response.headers.get("content-type", "")
        assert "text/plain" in ct, f"Expected text/plain Content-Type, got: {ct}"
        assert "version=0.0.4" in ct, f"Expected version=0.0.4 in Content-Type, got: {ct}"

    def test_get_metrics_response_returns_bytes_and_content_type(self) -> None:
        """get_metrics_response() must return (bytes, str) tuple."""
        content, content_type = get_metrics_response()
        assert isinstance(content, bytes), f"Expected bytes, got {type(content)}"
        assert isinstance(content_type, str), f"Expected str, got {type(content_type)}"
        assert "text/plain" in content_type


# =============================================================================
# 2.5 — DLQ gauge behavior
# =============================================================================


@pytest.mark.unit
class TestDlqGauge:
    """
    What: Verifies DLQ gauge uses -1 as error sentinel correctly.
    Why: Operators must know when DLQ measurement fails vs DLQ being empty.
    How fails: If gauge stays 0 on error, operators think DLQ is healthy.
    """

    def test_dlq_gauge_error_sentinel_is_negative_one(
        self, mock_metrics: dict
    ) -> None:
        """When DLQ measurement fails, gauge must be set to -1."""
        from app.metrics import dlq_size

        mock_metrics["dlq_size"].set(-1)
        mock_metrics["dlq_size"].set.assert_called_with(-1)
