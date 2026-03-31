# STATE — Async Jobs System

## Current Position
- **Phase**: 1 (Scaffold)
- **Status**: In Progress

## Key Decisions
- Redis como broker E result backend (simplicidade + Redis Streams como upgrade path)
- Celery 5.x (não ARQ/Dramatiq) por maturidade e ecossistema
- SQLAlchemy 2.x async com Alembic (nunca metadata.create_all)
- Pydantic v2 com `extra='forbid'` em todos os schemas
- structlog para logs JSON com campos obrigatórios
- OpenTelemetry para traces (OTEL collector → Loki/Grafana)

## Blockers
- Nenhum

## Notes
- suite_test.md é a fonte de verdade: testes não mudam, código satisfaz testes
- Cobertura mínima: 90% statements, 85% branches
