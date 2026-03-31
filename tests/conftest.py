"""
Central test fixtures for the Async Jobs System test suite.

Fixture scopes:
- session: containers started once, shared across all tests
- module: shared within a test module
- function: recreated per test for full isolation

Source of truth: suite_test.md — DO NOT modify test contracts.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import structlog
from freezegun import freeze_time
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = structlog.get_logger(__name__)

# =============================================================================
# Session-scoped: Docker containers (shared across entire test run)
# =============================================================================


@pytest.fixture(scope="session")
def redis_container() -> Generator[Any, None, None]:
    """
    Start a Redis container for the test session.
    Skipped if testcontainers is unavailable.
    """
    try:
        from testcontainers.redis import RedisContainer
    except ImportError:
        pytest.skip("testcontainers not installed")

    with RedisContainer("redis:7-alpine") as rc:
        yield rc


@pytest.fixture(scope="session")
def postgres_container() -> Generator[Any, None, None]:
    """
    Start a PostgreSQL container for the test session.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")

    with PostgresContainer("postgres:16-alpine") as pc:
        yield pc


@pytest.fixture(scope="session")
def apply_migrations(postgres_container: Any) -> None:
    """
    Run Alembic migrations programmatically once per session.
    Ensures the schema matches production migrations (no metadata.create_all).
    """
    from alembic.config import Config
    from alembic import command

    db_url = postgres_container.get_connection_url()
    # Convert to async URL for SQLAlchemy
    async_url = db_url.replace("postgresql://", "postgresql+asyncpg://")

    os.environ["DATABASE_URL"] = async_url

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", async_url)
    command.upgrade(alembic_cfg, "head")


@pytest.fixture(scope="session")
def db_engine(apply_migrations: None) -> Generator[AsyncEngine, None, None]:
    """
    Async engine connected to the test Postgres container.
    """
    url = os.environ["DATABASE_URL"]
    engine = create_async_engine(url, pool_pre_ping=True)
    yield engine
    asyncio.get_event_loop().run_until_complete(engine.dispose())


@pytest.fixture(scope="session")
def docker_client() -> Generator[Any, None, None]:  # type: ignore[misc]
    """
    Docker SDK client for chaos tests. Skip if Docker is not available.
    """
    try:
        import docker
        client = docker.from_env()
        client.ping()
        yield client
        client.close()
    except Exception:
        pytest.skip("Docker socket not available — skipping chaos tests")


@pytest.fixture(scope="session")
def celery_app_test() -> Any:
    """
    Celery application configured for testing with ALWAYS_EAGER mode.
    """
    from workers.celery_app import celery_app
    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
        result_backend="cache+memory://",
    )
    return celery_app


# =============================================================================
# Function-scoped: DB session with automatic rollback
# =============================================================================


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """
    Provide a test DB session that rolls back after each test.
    This ensures test isolation without DROP/CREATE overhead.
    """
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            yield session
            await session.rollback()


@pytest_asyncio.fixture(scope="function")
async def clean_tables(db_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """
    TRUNCATE all tables before each integration test.
    Faster than rollback for tests that commit.
    """
    yield
    from sqlalchemy import text
    async with db_engine.connect() as conn:
        await conn.execute(text("TRUNCATE TABLE jobs RESTART IDENTITY CASCADE"))
        await conn.commit()


# =============================================================================
# Function-scoped: HTTP client
# =============================================================================


@pytest_asyncio.fixture(scope="function")
async def async_client(
    postgres_container: Any, redis_container: Any
) -> AsyncGenerator[AsyncClient, None]:
    """
    Async HTTPX client connected to the FastAPI app.
    Uses real containers for I/O.
    """
    from app.main import create_app

    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        yield client


# =============================================================================
# Function-scoped: Mock metrics (unit tests — no Prometheus side effects)
# =============================================================================


@pytest.fixture(scope="function")
def mock_metrics() -> Generator[dict[str, Any], None, None]:
    """
    Patch all Prometheus collectors to prevent cross-test contamination.
    Returns dict of mocked objects for assertion.
    """
    mocks: dict[str, Any] = {
        "jobs_total": MagicMock(),
        "job_duration_seconds": MagicMock(),
        "dlq_size": MagicMock(),
        "active_workers": MagicMock(),
    }
    with (
        patch("app.metrics.jobs_total", mocks["jobs_total"]),
        patch("app.metrics.job_duration_seconds", mocks["job_duration_seconds"]),
        patch("app.metrics.dlq_size", mocks["dlq_size"]),
        patch("app.metrics.active_workers", mocks["active_workers"]),
        patch("workers.base_task.jobs_total", mocks["jobs_total"]),
        patch("workers.base_task.job_duration_seconds", mocks["job_duration_seconds"]),
        patch("workers.base_task.dlq_size", mocks["dlq_size"]),
    ):
        yield mocks


# =============================================================================
# Function-scoped: Auth fixtures (JWT)
# =============================================================================


@pytest.fixture(scope="function")
def authenticated_headers() -> dict[str, str]:
    """
    Valid JWT token for a test user.
    """
    from jose import jwt
    from app.config import get_settings
    from datetime import datetime, timezone, timedelta

    cfg = get_settings()
    payload_data = {
        "sub": "test-user-id",
        "email": "test@example.com",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload_data, cfg.jwt_secret_key, algorithm=cfg.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def malicious_user_headers() -> dict[str, str]:
    """
    Valid JWT token for a DIFFERENT test user (for IDOR tests).
    """
    from jose import jwt
    from app.config import get_settings
    from datetime import datetime, timezone, timedelta

    cfg = get_settings()
    payload_data = {
        "sub": "malicious-user-id",
        "email": "attacker@evil.com",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload_data, cfg.jwt_secret_key, algorithm=cfg.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# Function-scoped: Time control
# =============================================================================


@pytest.fixture(scope="function")
def freeze_time_fixture() -> Generator[Any, None, None]:
    """
    freezegun context for controlling time in retry backoff tests.
    """
    with freeze_time("2026-03-31 12:00:00") as frozen:
        yield frozen
