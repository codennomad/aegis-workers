# ADR 002 — Estratégia de Testes

**Status:** Aceito  
**Data:** 2026-03-31  
**Autores:** Equipe de plataforma

---

## Contexto

Um sistema de jobs assíncronos exige testes que validem:
1. Lógica de negócio isolada (unit)
2. Integração real com Postgres e Redis (integration)
3. Comportamento sob falhas de infraestrutura (chaos)
4. Resistência a ataques conhecidos (security)

Este ADR documenta as decisões sobre cada camada e os trade-offs aceitos.

---

## Pirâmide de testes adotada

```
         ┌─────────────┐
         │  Security   │  7 arquivos — OWASP Top 10
         │  (7 testes) │  Lento – 10–30s
         ├─────────────┤
         │    Chaos    │  4 cenários — falha de infra
         │  (4 testes) │  Muito lento – 60–300s, Docker obrigatório
         ├─────────────┤
         │ Integration │  5 cenários — containers reais
         │  (5 testes) │  Médio – 5–30s por teste
         ├─────────────┤
         │    Unit     │  15 cenários — isolados, mocks
         │ (15 testes) │  Rápido – < 1s por teste
         └─────────────┘
```

**Regra de ouro:** Testes de camadas menores (unit) devem ser executados mais frequentemente.

---

## Decisão 1 — testcontainers em vez de mocks para integração

### Alternativas

| Abordagem | Velocidade | Fidelidade | Manutenção |
|---|---|---|---|
| Mocks de DB (MagicMock) | Muito rápida | Baixa — não detecta bugs de SQL | Alta — mocks ficam desatualizados |
| SQLite em memória | Rápida | Média — dialeto diferente do Postgres | Média |
| **testcontainers** | Média (5–30s setup) | **Alta — idêntico à produção** | Baixa — automanaged |

### Decisão

**testcontainers** foi escolhido para integração porque:

1. **Fidelidade**: Usa `postgres:16-alpine` e `redis:7-alpine` — idênticos ao `docker-compose.yml` de produção.
2. **Isolamento**: Cada suíte de integração recebe containers dedicados (scope=`module`). Sem estado compartilhado entre testes.
3. **CI/CD**: GitHub Actions suporta Docker-in-Docker. CI pipeline já configurado.
4. **Bugs de SQL detectados**: Constraints, tipos JSONB, índices únicos — todos funcionam como em prod.

### Trade-offs aceitos

- **Mais lentos** que unit tests: cada teste de integração leva 5–30s vs < 1s para unit.
- **Requer Docker**: Não pode ser executado em ambientes sem Docker daemon. Mitigado marcando com `@pytest.mark.integration` e separando no CI.

---

## Decisão 2 — Testes de chaos com Docker SDK (não pytest-chaos)

### Decisão

Chaos tests usam `docker.from_env()` para parar/iniciar containers diretamente.

**Motivo**: pytest-chaos e Chaos Monkey são para clusters. Nossa granularidade é container-level, e o Docker SDK é suficiente.

### SLA Contracts definidos nos chaos tests

| Cenário | Comportamento esperado | SLA |
|---|---|---|
| Redis restart | Sistema se recupera automaticamente | ≤ 30s |
| Postgres stop | `/health` retorna 503 | Imediato (< 2s) |
| Redis stop | Jobs criados antes persistem no DB | 0 jobs perdidos |
| Task re-delivery | `execution_count` ≤ 1 (sem duplicatas) | 100% das vezes |

---

## Decisão 3 — SAST (bandit + pip-audit) vs Pentest dinâmico

### Abordagem em duas camadas

```
Camada 1 — SAST (estático, < 30s):
  bandit -r app/ workers/ --severity-level high
  pip-audit --format json
  detect-secrets scan --all-files

Camada 2 — Pentest dinâmico (app rodando, > 30s):
  SQL injection payloads via httpx
  JWT alg:none attack
  SSRF payload injection
  Mass assignment (extra='forbid')
  IDOR cross-user access
  DoS via payload > 1MB
  Task name injection
```

**Por que ambos?**
- SAST encontra bugs *no código* (code smell, imports inseguros, hardcoded secrets)
- Pentest encontra bugs *no comportamento* (resposta HTTP errada para payload malicioso)

---

## Decisão 4 — 90% de cobertura como piso

### Configuração

```toml
# pyproject.toml
[tool.pytest.ini_options]
addopts = "--cov=app --cov=workers --cov-fail-under=90"
```

### Por que 90% e não 100%?

| Cobertura | Trade-off |
|---|---|
| 100% | Incentiva escrever testes para código morto, não para cenários reais |
| **90%** | Cobre todos os happy paths + principais error paths sem overhead artificial |
| < 80% | Indica lacunas críticas em error handling e edge cases |

### O que os 10% restantes representam

Os 10% não cobertos são tipicamente:
- Fallbacks impossíveis de alcançar (e.g., `raise NotImplementedError` em abstract)
- Condições de race condition que só ocorrem em produção
- Branches de OS-specific code

**Regra**: Qualquer linha contribuindo para uma funcionalidade de negócio DEVE ser coberta.

---

## Decisão 5 — Marcadores pytest para execução seletiva

```python
@pytest.mark.unit          # Rápido, sem I/O externo
@pytest.mark.integration   # Requer Docker, testcontainers
@pytest.mark.chaos         # Requer Docker + containers rodando
@pytest.mark.security      # Requer app rodando, pode ser lento
```

### Execução no development

```bash
# Só unit (segundos)
pytest -m unit

# Unit + integration (minutos)
pytest -m "unit or integration"

# Suite completa (CI)
pytest -m "unit or integration or security"

# Chaos (só em CI/CD ou manualmente)
pytest -m chaos
```

---

## Consequências

- Todo novo endpoint deve ter pelo menos 1 unit test + 1 integration test
- Todo novo Celery task deve ter teste de backoff + idempotência
- Qualquer CVE detectado por `pip-audit` bloqueia o merge (CI gate)
- Chaos tests rodam apenas em `push` para `main` (não em PRs feature branches)
