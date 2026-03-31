# Requirements — Async Jobs System

## V1 (This Milestone)

### API
- [ ] R01 — POST /jobs com idempotency_key obrigatório
- [ ] R02 — GET /jobs/{id} com status, retries, estimated_next_run
- [ ] R03 — POST /jobs/{id}/cancel (apenas PENDING)
- [ ] R04 — POST /jobs/{id}/retry (reenvio da DLQ)
- [ ] R05 — GET /health com latência Redis + Postgres

### Workers
- [ ] R06 — BaseTask com autoretry, backoff exponencial `2**retries*10`
- [ ] R07 — DLQ configurada como fila separada `dlq`
- [ ] R08 — Idempotência via Redis cache por idempotency_key
- [ ] R09 — Late ack, prefetch=1, serializer JSON
- [ ] R10 — on_failure: DLQ + status banco + métrica
- [ ] R11 — on_success: status banco + métrica + duração

### Observability
- [ ] R12 — Counter `jobs_total` com labels (task_name, status)
- [ ] R13 — Histogram `job_duration_seconds` com buckets customizados
- [ ] R14 — Gauge `dlq_size` atualizado por execução
- [ ] R15 — Gauge `active_workers` via Celery inspect
- [ ] R16 — GET /metrics exposto (Prometheus format)
- [ ] R17 — Alertas: DLQSizeHigh, ErrorRateHigh, WorkerDown

### Infrastructure
- [ ] R18 — docker-compose com: fastapi, celery-worker (--concurrency=4), celery-beat, celery-flower, redis, postgres, prometheus, grafana, loki, promtail
- [ ] R19 — Health checks em todos os serviços
- [ ] R20 — Volumes nomeados e rede interna isolada

### Testing (fonte de verdade: suite_test.md)
- [ ] R21 — tests/unit/test_base_task.py (5 cenários)
- [ ] R22 — tests/unit/test_metrics.py (5 cenários)
- [ ] R23 — tests/unit/test_jobs_routes.py (5 cenários)
- [ ] R24 — tests/integration/test_job_lifecycle.py (5 cenários)
- [ ] R25 — tests/chaos/test_resilience.py (4 cenários)
- [ ] R26 — tests/security/test_vulnerabilities.py (6 cenários)
- [ ] R27 — tests/security/test_pentest.py (7 cenários)
- [ ] R28 — tests/conftest.py com todas as fixtures obrigatórias
- [ ] R29 — pyproject.toml com pytest config + coverage 90%
- [ ] R30 — .github/workflows/test.yml com pipeline CI completo

### Docs
- [ ] R31 — docs/decisions/001-arquitetura.md (ADR)
- [ ] R32 — docs/decisions/002-estrategia-de-testes.md (ADR)
- [ ] R33 — docs/chaos/RESULTS.md (template)

## V2 (Future)

- Rate limiting por IP
- Multi-tenancy: jobs isolados por usuário
- gRPC streaming para status em tempo real
- Event sourcing para auditoria completa
- Kubernetes deployment com HPA
