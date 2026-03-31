# ADR 001 — Decisões de Arquitetura do Sistema de Jobs Assíncronos

**Status:** Aceito  
**Data:** 2026-03-31  
**Autores:** Equipe de plataforma

---

## Contexto

Precisamos de um sistema de processamento assíncrono de jobs que:
- Suporte alto volume (SLA: ≥1000 jobs/minuto em produção)
- Seja observável (Prometheus, Grafana, Loki, OpenTelemetry)
- Seja tolerante a falhas (retry, DLQ, idempotência)
- Seja auditável (rastreamento de estado, parent_job_id)
- Seja seguro (OWASP Top 10 mitigado)

---

## Decisão 1 — Celery em vez de ARQ ou Dramatiq

### Alternativas consideradas

| Critério | **Celery 5.x** | ARQ | Dramatiq |
|---|---|---|---|
| Maturidade | 15+ anos, 23k stars | 3 anos, 2.8k stars | 5 anos, 4.3k stars |
| Broker suportado | Redis, RabbitMQ, SQS | Apenas Redis | Redis, RabbitMQ |
| Suporte a DLQ | Nativo (dead letter routing) | Manual | Plugin |
| Flower (UI) | Nativo | Não incluso | Não incluso |
| Beat (agendamento) | Nativo | Nativo | Manual |
| Async nativo | Não (hybrid via asyncio.run) | Sim | Não |
| Suporte a k8s | Maduro | Experimental | Estável |

### Decisão

**Celery 5.x** foi escolhido pelos seguintes motivos:

1. **Ecossistema e doc extensa**: Issues e soluções documentadas para todos os edge cases de produção.
2. **Flower UI**: Visibilidade imediata de workers, filas e tasks sem custo extra de implementação.
3. **DLQ nativo**: Suporte a dead letter queues via direct exchange com TTL de 7 dias.
4. **Beat scheduler**: Necessário para `update_dlq_metrics` periódico (30s).
5. **ARQ é async-only**: Em workers CPU-bound, async nativo cria contenção de event loop.

### Trade-offs aceitos

- **Celery é síncrono**: Workers usam `asyncio.run()` para chamar código async do DB. Aceitável porque a operação de DB é rápida (< 10ms) e não justifica converter todo o worker para async.
- **Celery não é cloud-native por padrão**: Compensado com `task_acks_late=True` e `task_reject_on_worker_lost=True`.

---

## Decisão 2 — Redis em vez de RabbitMQ como broker

### Alternativas consideradas

| Critério | **Redis** | RabbitMQ |
|---|---|---|
| Latência de enfileiramento | < 1ms | 2–5ms |
| Redelivery garantido | Via `task_acks_late` | Nativo (AMQP acks) |
| Persistência | RDB + AOF configurável | Durável por padrão |
| Infraestrutura dupla | Redis serve como broker E result backend | Broker separado do backend |
| Operação | Simples, sem plugins | Requer gestão de exchanges |
| Monitoramento | redis-cli, RedisInsight | Management Plugin |

### Decisão

**Redis** foi escolhido porque:

1. **Infraestrutura única**: Redis serve como broker (`/0`) e result backend (`/1`), eliminando um componente de infra.
2. **Latência**: Sub-milissegundo para enfileirar e dequeue.
3. **Persistência suficiente**: `appendonly yes` + `save 60 1000` cobre nosso RTO de 30s.
4. **Idempotência**: Redis SETEX para cache de idempotência é nativo (sem overhead extra).

### Trade-offs aceitos

- **Redis não tem AMQP**: Mensagens podem ser perdidas se o worker morrer antes do ack. Mitigado com `task_acks_late=True` + `task_reject_on_worker_lost=True`.
- **Redis tem limite de memória**: Monitorado via `dlq_size` gauge e alertas Prometheus.

---

## Decisão 3 — Alembic para migrações (nunca `create_all`)

### Regra imutável

**`Base.metadata.create_all()` está PROIBIDO em produção.**

| Abordagem | Por que usar | Por que NÃO usar |
|---|---|---|
| `metadata.create_all()` | Rápido para protótipos | Sem rastreabilidade, sem rollback, sem down migrations |
| **Alembic** | Histórico de versões, rollback por migration, CI/CD seguro | Requer geração manual |

### Regra para novos modelos

1. Modificar `app/models/`
2. Gerar: `alembic revision --autogenerate -m "description"`
3. Revisar o arquivo gerado em `alembic/versions/`
4. Aplicar: `alembic upgrade head`
5. Nunca fazer merge sem migration correspondente

---

## Decisão 4 — Plano de escalabilidade 10x

### Estado atual (v1)

```
1x FastAPI (1 container, 4 uvicorn workers)
4x Celery workers (--concurrency=4 each = 16 concurrent tasks)
1x Redis (single node)
1x Postgres (single node)
```

Capacidade estimada: **~500–1000 jobs/minuto**

### Plano 10x

```
Fase 1 — Horizontal scaling (sem mudanças de código):
  - FastAPI: 3 instâncias atrás de load balancer (nginx/traefik)
  - Celery workers: de 1 para 10 containers (autoscale via KEDA)
  - Redis: Sentinel ou Cluster mode
  - Postgres: Read replica para SELECT de status polling

Fase 2 — Filas priorizadas (mudança de config Celery):
  - high_priority queue: emails transacionais
  - default queue: emails bulk
  - dlq: mortos aguardando revisão

Fase 3 — Backend swap se Redis atingir limite:
  - Broker: migrar para RabbitMQ com clustering
  - Mantém mesma API Celery (apenas muda CELERY_BROKER_URL)

Capacidade estimada 10x: ~5000–10000 jobs/minuto
```

---

## Consequências

- Workers devem sempre usar `task_acks_late=True` (já configurado)
- Migrations sempre via Alembic
- Redis nunca pode ser substituído por SQLite/in-memory em produção
- Qualquer novo task_name deve ser adicionado a `Settings.allowed_task_names`
