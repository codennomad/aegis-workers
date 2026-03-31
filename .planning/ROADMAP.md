# Roadmap — Async Jobs System v1.0

## Phase 1 — Project Scaffold + Core Models ✅ Planned
- 1-01: Estrutura de diretórios, pyproject.toml, .gitignore, .env.example
- 1-02: Models SQLAlchemy (Job), Alembic setup, migration inicial
- 1-03: Schemas Pydantic v2 (request/response), app/config.py
- 1-04: DB session factory async + app/main.py bootstrap

## Phase 2 — Workers (Celery + BaseTask + DLQ)
- 2-01: workers/celery_app.py configuração completa
- 2-02: workers/base_task.py com retry/backoff/idempotência
- 2-03: workers/tasks/ com tasks de exemplo (email_task)

## Phase 3 — API Routes + Metrics
- 3-01: app/metrics.py (Counter, Histogram, Gauges)
- 3-02: app/routes/jobs.py (POST, GET, cancel, retry, health)
- 3-03: app/services/job_service.py (lógica de negócio)

## Phase 4 — Infrastructure
- 4-01: docker-compose.yml completo (11 serviços)
- 4-02: infra/prometheus/ (prometheus.yml + alerts.yml)
- 4-03: infra/grafana/ (datasources + dashboard)
- 4-04: infra/loki/ + infra/promtail/ configs

## Phase 5 — Tests (Unit + Integration)
- 5-01: tests/conftest.py com todas as fixtures
- 5-02: tests/unit/ (test_base_task, test_metrics, test_jobs_routes)
- 5-03: tests/integration/test_job_lifecycle.py

## Phase 6 — Tests (Chaos + Security)
- 6-01: tests/chaos/test_resilience.py
- 6-02: tests/security/test_vulnerabilities.py
- 6-03: tests/security/test_pentest.py

## Phase 7 — Docs + CI Pipeline
- 7-01: docs/decisions/001-arquitetura.md
- 7-02: docs/decisions/002-estrategia-de-testes.md
- 7-03: docs/chaos/RESULTS.md
- 7-04: .github/workflows/test.yml
