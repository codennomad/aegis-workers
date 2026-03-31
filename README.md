# Aegis Workers

> Production-grade async job processing system built with FastAPI, Celery, and Redis.
> Designed to demonstrate senior-level engineering judgment — not just working code.

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com)
[![Celery](https://img.shields.io/badge/Celery-5.x-37814A.svg)](https://docs.celeryq.dev)
[![Coverage](https://img.shields.io/badge/coverage-target%20100%25-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## What this is

Aegis Workers is a distributed job queue system built from first principles — not a tutorial app. Every design decision reflects trade-offs you'd evaluate in a real production system: at-least-once delivery guarantees, idempotency enforcement across retries, structured observability, and a full Dead Letter Queue lifecycle.

The goal was to produce a codebase that a senior engineer reviewing a PR would find **no obvious omissions in**.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          API Layer                              │
│   FastAPI  ·  slowapi (rate limiting)  ·  OpenTelemetry OTLP   │
└────────────────────────────┬────────────────────────────────────┘
                             │ enqueue (task_name whitelist)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Message Broker                            │
│                Redis (broker /0  ·  result backend /1)          │
└──────────────────┬────────────────────────┬─────────────────────┘
                   │                        │
          ┌────────▼────────┐    ┌──────────▼──────────┐
          │  default queue  │    │     dlq queue        │
          │  (workers × 4)  │    │  (manual retry API)  │
          └────────┬────────┘    └──────────────────────┘
                   │ on_failure (max_retries exceeded)
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Persistence Layer                          │
│        PostgreSQL (SQLAlchemy 2.x async · Alembic migrations)   │
└─────────────────────────────────────────────────────────────────┘
                             │
┌─────────────────────────────────────────────────────────────────┐
│                     Observability Stack                         │
│   Prometheus  ·  Grafana  ·  Loki  ·  Promtail  ·  Flower      │
└─────────────────────────────────────────────────────────────────┘
```

### Core guarantees

| Property | Mechanism |
|---|---|
| At-least-once delivery | `task_acks_late=True` + `task_reject_on_worker_lost=True` |
| Exactly-once semantics (best-effort) | `idempotency_key` checked before task execution |
| Bounded retries | `max_retries=5`, exponential backoff `2ⁿ × 10s` |
| DLQ isolation | Failed-beyond-retries tasks routed to dedicated `dlq` queue |
| No task injection | `allowed_task_names` whitelist validated before enqueue (OWASP A03) |
| No stack trace leakage | Global exception handler always returns `{"detail": "Internal server error"}` |

---

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI 0.115 · Pydantic v2 · Python 3.12 |
| Workers | Celery 5.x · Redis broker + result backend |
| Persistence | PostgreSQL 16 · SQLAlchemy 2.x async · Alembic |
| Observability | Prometheus · Grafana · Loki · Promtail · OpenTelemetry OTLP |
| Rate limiting | slowapi (per-IP, configurable via `RATE_LIMIT_PER_MINUTE`) |
| Structured logs | structlog (JSON, with `job_id`, `trace_id`, `worker_id`) |
| Tests | pytest · pytest-asyncio · testcontainers · HTTPX async client |
| Infra | Docker Compose (single `docker compose up` to start everything) |

---

## Getting started

### Prerequisites

- Docker ≥ 24 and Docker Compose ≥ 2.20
- Python 3.12 (for local development without Docker)

### Run with Docker

```bash
git clone git@github.com:codennomad/aegis-workers.git
cd aegis-workers

cp .env.example .env          # review defaults before changing

docker compose up --build     # all 11 services start with health checks
```

Services come up in dependency order. The API is available at `http://localhost:8000` once healthy.

### Local development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate          # or .venv\Scripts\activate on Windows

pip install -e ".[dev]"

# Start dependencies only
docker compose up postgres redis -d

# Run migrations
alembic upgrade head

# Start API
uvicorn app.main:app --reload

# Start worker (separate terminal)
celery -A workers.celery_app worker --loglevel=info --concurrency=4
```

### Environment variables

See [.env.example](.env.example) for all available settings. Required overrides for production:

| Variable | Description |
|---|---|
| `SECRET_KEY` | ≥ 32 random bytes |
| `JWT_SECRET_KEY` | ≥ 32 random bytes |
| `DATABASE_URL` | PostgreSQL async DSN |
| `REDIS_URL` | Redis connection string |
| `ALLOWED_TASK_NAMES` | Comma-separated whitelist |

---

## API reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/jobs` | Submit a job. Returns 201 (new) or 200 (idempotency hit) |
| `GET` | `/jobs/{id}` | Fetch status, retries, estimated next run |
| `POST` | `/jobs/{id}/cancel` | Revoke a PENDING job from Celery |
| `POST` | `/jobs/{id}/retry` | Re-enqueue a DLQ job manually |
| `GET` | `/health` | Check Redis and PostgreSQL connectivity with latency |
| `GET` | `/metrics` | Prometheus scrape endpoint |

### Idempotency

Supply `idempotency_key` on `POST /jobs`. Duplicate submissions with the same key return the original job with HTTP 200 — no double enqueue. The key is enforced at both the API layer (DB unique check) and the worker layer (pre-execution guard).

### DLQ lifecycle

```
PENDING → RUNNING → FAILED (retry n)
                        │ max_retries exceeded
                        ▼
                       DLQ
                        │ POST /jobs/{id}/retry
                        ▼
                    new PENDING (parent_job_id = original)
```

---

## Observability

| Signal | Where |
|---|---|
| Metrics | `http://localhost:9090` (Prometheus) |
| Dashboards | `http://localhost:3000` (Grafana — pre-provisioned) |
| Logs | Grafana → Explore → Loki |
| Worker monitor | `http://localhost:5555` (Flower) |
| Traces | OTLP → collector (configurable via `OTEL_EXPORTER_OTLP_ENDPOINT`) |

### Prometheus alerts

Defined in [infra/prometheus/alerts.yml](infra/prometheus/alerts.yml):

| Alert | Condition | Severity |
|---|---|---|
| `DLQSizeHigh` | `dlq_size > 10` for 2m | warning |
| `ErrorRateHigh` | error rate > 1% over 5m | critical |
| `WorkerDown` | `active_workers == 0` for 2m | critical |

---

## Testing

```bash
# Unit tests (no external services needed)
pytest tests/unit -v

# Integration tests (requires Docker — starts real Postgres + Redis)
pytest tests/integration -v

# Chaos tests (kill workers mid-execution, network partitions)
pytest tests/chaos -v

# Security tests (OWASP Top 10 surface)
pytest tests/security -v

# All tests with coverage
pytest --cov=app --cov=workers --cov-report=term-missing
```

### Test strategy

The test suite is the source of truth for this codebase — application code was written to satisfy the tests, not the other way around.

| Layer | Focus |
|---|---|
| Unit | Metrics correctness, base task retry/DLQ logic, route contract |
| Integration | Full job lifecycle: create → enqueue → execute → status |
| Chaos | Worker crash recovery, Redis disconnection, DB unavailability |
| Security | SQL injection surface, XSS payloads, task injection attempts |

---

## Architecture Decision Records

- [ADR 001 — Why Celery over ARQ/Dramatiq, why Redis over RabbitMQ](docs/decisions/001-arquitetura.md)
- [ADR 002 — Test strategy and the Nyquist principle](docs/decisions/002-estrategia-de-testes.md)

---

## What I'd do differently

> This section exists because honest post-mortems are more valuable than polished marketing. A senior reviewer should never have to wonder "did the author think about X?" — this section answers that preemptively.

### 1. Replace `asyncio.run()` in workers with a proper async worker model

Celery workers are sync. When `on_success` and `on_failure` need to write to PostgreSQL, they call `asyncio.run(...)` on an async SQLAlchemy session. This works, but it's a leaky abstraction — each task spawns a new event loop, which is wasteful and defeats connection pooling.

**Better approach:** Use [Taskiq](https://github.com/taskiq-python/taskiq) or an async-native Celery alternative. If staying on Celery, refactor worker hooks to maintain a long-lived event loop per worker process via `celery.signals.worker_init`.

### 2. Enforce distributed idempotency with Redis, not just PostgreSQL

The current idempotency check is a `SELECT ... WHERE idempotency_key = ?`. Under burst load, two concurrent requests with the same key can both pass the check before either commits. This is a classic TOCTOU race.

**Better approach:** Acquire a Redis lock (`SET NX PX`) on the idempotency key before the DB write. Release it after commit. This reduces the race window to near-zero without serializable transactions.

### 3. Add a proper outbox pattern for enqueue reliability

The current flow is: `INSERT job → COMMIT → send_task(...)`. If the process crashes between commit and `send_task`, the job exists in the DB but was never enqueued. The worker never sees it.

**Better approach:** Implement a [transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html): write to an `outbox` table in the same transaction, then a polling process (or CDC via Debezium) publishes to Redis. Guarantees exactly-once enqueue even on crash.

### 4. API authentication is scaffolded but not enforced

`jwt_secret_key` and `jwt_algorithm` exist in `Settings`, but there is no auth middleware or `Depends(get_current_user)` on any route. For a portfolio project the endpoints are intentionally open, but in production this would be a P0 security gap.

**Better approach:** Add a `Bearer` token dependency on all mutating routes. Use a short-lived JWT with `sub` = service account ID. The `/metrics` and `/health` endpoints stay open for scraper access.

### 5. The test suite uses wall-clock time in chaos tests

Some chaos tests assert timing properties (e.g., "retry happens within N seconds"). These are flaky under CPU contention in CI runners.

**Better approach:** Inject a `Clock` abstraction into `BaseTask` and use a fake clock in tests. This eliminates all time-based flakiness without losing behavioral coverage.

### 6. No schema versioning for the job payload

`payload: dict[str, Any]` is completely untyped in the DB. If `send_email` changes its expected keys, old jobs sitting in the DLQ will fail deserialization silently.

**Better approach:** Store `payload_schema_version` alongside the payload. Workers check the version and either migrate the payload or route to a dedicated schema-migration handler before execution.

### 7. Celery Beat as a separate container is a single point of failure

The `celery-beat` container manages periodic tasks (DLQ metric refresh every 30s). If it crashes and doesn't restart fast enough, metrics go stale.

**Better approach:** For resilience, use `redbeat` (a Redis-backed Beat scheduler with leader election) so any worker process can take over Beat's role without a dedicated container.

---

## Project structure

```
.
├── app/
│   ├── config.py              # Pydantic Settings v2 — all config from env
│   ├── main.py                # FastAPI factory + lifespan + middleware
│   ├── metrics.py             # Prometheus counters, histograms, gauges
│   ├── db/session.py          # Async SQLAlchemy engine + session factory
│   ├── models/job.py          # ORM model with full lifecycle fields
│   ├── schemas/job.py         # Request/response Pydantic v2 schemas
│   ├── routes/jobs.py         # Route handlers (orchestration only)
│   └── services/job_service.py# All business logic
├── workers/
│   ├── celery_app.py          # Celery config: DLQ routing, late ack, prefetch=1
│   ├── base_task.py           # BaseTask: retry, backoff, DLQ, idempotency guard
│   └── tasks/email_task.py    # Example task implementation
├── alembic/                   # Migration scripts (no create_all, ever)
├── infra/
│   ├── prometheus/            # prometheus.yml + alerts.yml
│   ├── grafana/               # Pre-provisioned datasources + dashboards
│   ├── loki/                  # Loki config
│   └── promtail/              # Log scraping config
├── tests/
│   ├── unit/                  # Pure unit tests, no I/O
│   ├── integration/           # Testcontainers: real Postgres + Redis
│   ├── chaos/                 # Fault injection tests
│   └── security/              # OWASP Top 10 surface coverage
├── docs/decisions/            # Architecture Decision Records
├── docker-compose.yml         # Full stack: 11 services, health checks, named volumes
└── pyproject.toml             # Single source of truth for dependencies + tooling
```

---

## License

MIT — use it, learn from it, build on it.
