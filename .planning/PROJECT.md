# Async Jobs System — Production-Grade Portfolio Project

## Vision

Sistema de jobs assíncronos de nível de produção construído para demonstrar maturidade
de engenharia para o mercado internacional sênior. O sistema processa tarefas em background
com confiabilidade, observabilidade completa e resiliência comprovada por testes.

## Goals

1. **Functional**: API REST completa para submissão, cancelamento e reenvio de jobs
2. **Reliable**: Retries com backoff exponencial, DLQ, idempotência, late ack
3. **Observable**: Métricas Prometheus, traces OpenTelemetry, logs estruturados Loki
4. **Tested**: Suite completa: unit, integration, chaos engineering, security/pentest
5. **Deployable**: Um único `docker compose up` sobe tudo

## Tech Stack

- **API**: FastAPI + Pydantic v2 + SQLAlchemy 2.x async + Alembic
- **Workers**: Celery 5.x + Redis (broker + result backend)
- **Observability**: Prometheus + Grafana + Loki + OpenTelemetry
- **Testing**: pytest + pytest-asyncio + testcontainers
- **Infra**: Docker Compose

## Constraints

- Nenhum `TODO`, `pass` onde há lógica, `unwrap` implícito
- Type hints 100% — mypy sem erros
- Logs estruturados JSON com: timestamp, level, job_id, trace_id, worker_id
- Migrations apenas via Alembic, nunca `metadata.create_all()`
- Cobertura mínima: 90% statements, 85% branches

## Source of Truth for Tests

O arquivo `suite_test.md` define contratos e comportamentos. **Os testes não podem ser
alterados, ignorados ou contornados.** O código é escrito para satisfazê-los.
