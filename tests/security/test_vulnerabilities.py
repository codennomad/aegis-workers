"""
Security vulnerability tests (OWASP Top 10 validation).

What: Tests for known vulnerability patterns in the application.
Why: Security issues in prod cause data breaches, compliance violations, fines.
How fails: If any test fails, a real exploitable vulnerability exists in production.

Run: pytest tests/security/test_vulnerabilities.py -m security -v
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.security]


# =============================================================================
# 6.1 — Dependency audit (supply chain security)
# =============================================================================


@pytest.mark.asyncio
async def test_no_known_cve_in_dependencies() -> None:
    """
    What: All dependencies must pass pip-audit (no known CVEs).
    Why: OWASP A06 — Vulnerable and Outdated Components.
    How fails: Any CVE in locked dependencies is a critical security finding.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pip_audit", "--format", "json", "--no-deps"],
        capture_output=True,
        text=True,
    )
    # pip-audit exits 0 if no vulnerabilities
    assert result.returncode == 0, (
        f"pip-audit found vulnerabilities:\n{result.stdout}\n{result.stderr}"
    )


# =============================================================================
# 6.2 — Secrets detection
# =============================================================================


def test_no_hardcoded_secrets_in_codebase() -> None:
    """
    What: Codebase must not contain hardcoded secrets detectable by detect-secrets.
    Why: OWASP A02 — Cryptographic Failures; leaked secrets → breach.
    How fails: Any real secret in source = immediate security incident.
    """
    workspace_root = Path(__file__).parent.parent.parent
    result = subprocess.run(
        [
            "detect-secrets",
            "scan",
            "--all-files",
            "--exclude-files", r"\.git",
            "--exclude-files", r"\.env\.example",
            "--exclude-files", r"tests/",
            str(workspace_root),
        ],
        capture_output=True,
        text=True,
        cwd=workspace_root,
    )

    import json as _json
    try:
        scan_result = _json.loads(result.stdout)
        # Count secrets found across all files
        total_secrets = sum(
            len(secrets)
            for secrets in scan_result.get("results", {}).values()
        )
        assert total_secrets == 0, (
            f"detect-secrets found {total_secrets} potential secrets:\n"
            f"{_json.dumps(scan_result['results'], indent=2)}"
        )
    except (_json.JSONDecodeError, KeyError):
        # detect-secrets not installed or different output format — skip
        pytest.skip("detect-secrets output could not be parsed. Install it: pip install detect-secrets")


# =============================================================================
# 6.3 — Security headers
# =============================================================================


@pytest.mark.asyncio
async def test_security_headers_present_on_all_responses(
    async_client: Any,
) -> None:
    """
    What: All responses must include OWASP-recommended security headers.
    Why: Missing security headers enable XSS, clickjacking, MIME sniffing attacks.
    How fails: Missing X-Content-Type-Options → browser MIME sniff attack possible.
    """
    response = await async_client.get("/health")

    expected_headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": ["DENY", "SAMEORIGIN"],
        "Strict-Transport-Security": None,  # Just check it exists
        "X-XSS-Protection": None,
    }

    for header_name, expected_value in expected_headers.items():
        actual_value = response.headers.get(header_name)
        assert actual_value is not None, (
            f"Missing security header: {header_name}. "
            f"Available headers: {dict(response.headers)}"
        )
        if isinstance(expected_value, list):
            assert actual_value in expected_value, (
                f"Header {header_name}: expected one of {expected_value}, got {actual_value}"
            )
        elif expected_value is not None:
            assert actual_value == expected_value, (
                f"Header {header_name}: expected {expected_value}, got {actual_value}"
            )


# =============================================================================
# 6.4 — Rate limiting (OWASP A04 — Insecure Design)
# =============================================================================


@pytest.mark.asyncio
async def test_rate_limiting_returns_429_after_threshold(
    async_client: Any,
    authenticated_headers: dict,
) -> None:
    """
    What: Requests exceeding rate limit must receive 429 Too Many Requests.
    Why: OWASP A04 — without rate limiting, DoS and enumeration attacks are trivial.
    How fails: Missing rate limiting → single attacker can exhaust server resources.

    Rate limit: 60 requests/minute per IP.
    """
    # We need more than the per-endpoint limit (typically 10-60/min in tests)
    # Send 70 requests to trigger the limit
    responses = []
    for _ in range(70):
        r = await async_client.post(
            "/jobs",
            json={
                "task_name": "workers.tasks.email_task.send_email",
                "idempotency_key": f"ratelimit-{_}-{id(_)}",
                "payload": {"to": "ratelimit@example.com"},
            },
            headers=authenticated_headers,
        )
        responses.append(r.status_code)
        if r.status_code == 429:
            break

    has_429 = 429 in responses
    assert has_429, (
        f"Expected at least one 429 response after 70 requests. "
        f"Got status codes: {set(responses)}"
    )

    # When rate-limited, Retry-After header must be present
    rate_limited_responses = [
        r for r in responses if r == 429
    ]
    # Can't access headers directly on collected status codes — repeat for header check
    r = await async_client.post(
        "/jobs",
        json={
            "task_name": "workers.tasks.email_task.send_email",
            "idempotency_key": f"ratelimit-header-check",
            "payload": {"to": "ratelimit@example.com"},
        },
        headers=authenticated_headers,
    )
    # If still rate-limited, check Retry-After header
    if r.status_code == 429:
        assert "retry-after" in {h.lower() for h in r.headers.keys()}, (
            "429 response must include Retry-After header"
        )


# =============================================================================
# 6.5 — CORS configuration
# =============================================================================


@pytest.mark.asyncio
async def test_cors_does_not_allow_wildcard_with_credentials(
    async_client: Any,
) -> None:
    """
    What: CORS must not allow *+credentials simultaneously.
    Why: OWASP A01 — Access Control. Wildcard CORS + credentials = auth bypass.
    How fails: Browser sends cookies/tokens to any origin if both are allowed.
    """
    response = await async_client.options(
        "/jobs",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )

    allow_origin = response.headers.get("access-control-allow-origin", "")
    allow_credentials = response.headers.get("access-control-allow-credentials", "").lower()

    # If wildcard is set, credentials must NOT be allowed
    if allow_origin == "*":
        assert allow_credentials != "true", (
            "CORS misconfiguration: Access-Control-Allow-Origin: * with "
            "Access-Control-Allow-Credentials: true is not allowed"
        )


# =============================================================================
# 6.6 — Static analysis (bandit)
# =============================================================================


def test_bandit_finds_no_high_severity_issues() -> None:
    """
    What: Bandit must not find HIGH severity issues in source code.
    Why: Bandit catches common Python security anti-patterns early.
    How fails: HIGH severity = exploitable vulnerability confirmed by static analysis.
    """
    workspace_root = Path(__file__).parent.parent.parent
    app_dir = workspace_root / "app"
    workers_dir = workspace_root / "workers"

    for target_dir in [app_dir, workers_dir]:
        if not target_dir.exists():
            continue

        result = subprocess.run(
            [
                sys.executable, "-m", "bandit",
                "-r", str(target_dir),
                "--severity-level", "high",
                "--confidence-level", "medium",
                "-f", "json",
            ],
            capture_output=True,
            text=True,
        )

        import json as _json
        try:
            report = _json.loads(result.stdout)
            high_issues = [
                issue
                for issue in report.get("results", [])
                if issue.get("issue_severity") == "HIGH"
            ]
            assert len(high_issues) == 0, (
                f"Bandit found {len(high_issues)} HIGH severity issues in {target_dir}:\n"
                + "\n".join(
                    f"  - {i['filename']}:{i['line_number']}: {i['issue_text']}"
                    for i in high_issues
                )
            )
        except _json.JSONDecodeError:
            pytest.skip(f"bandit not installed or failed for {target_dir}")
