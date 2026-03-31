# Resultado dos Testes de Chaos

**Data:** _preencher na execução_  
**Versão da aplicação:** _preencher (git SHA)_  
**Executor:** _preencher_  
**Ambiente:** _CI/CD | Local_

---

## Resumo

| Cenário | Status | Observado | Esperado | Duração |
|---|---|---|---|---|
| Redis restart | ⏳ | — | Recovery ≤ 30s | — |
| Redis stop — jobs perdidos | ⏳ | — | 0 jobs perdidos | — |
| Postgres stop — health | ⏳ | — | 503 imediato | — |
| Task re-delivery — duplicatas | ⏳ | — | execution_count ≤ 1 | — |

---

## Detalhes por cenário

### 5.1 — Redis Restart Recovery

**SLA:** Sistema deve se recuperar em ≤ 30 segundos após restart do Redis.

| Campo | Valor |
|---|---|
| Início do caos | — |
| Redis stopped at | — |
| Redis started at | — |
| Health check retornou 200 at | — |
| **Tempo de recovery** | **—** |
| SLA alcançado? | — |

**Observações:**
> _Preencher após execução_

**Jobs perdidos durante o caos:** 0 esperado / — observado

---

### 5.2 — Deduplicação via execution_count

**SLA:** `execution_count` deve ser ≤ 1 mesmo com re-delivery simulado.

| Campo | Valor |
|---|---|
| Jobs criados | — |
| Jobs com execution_count > 1 | — |
| Duplicatas detectadas | — |

**Observações:**
> _Preencher após execução_

---

### 5.3 — Postgres Stop → /health 503

**SLA:** /health deve retornar 503 em < 2 segundos após Postgres parar.

| Campo | Valor |
|---|---|
| Postgres stopped at | — |
| Primeiro 503 recebido | — |
| **Latência de detecção** | **—** |
| SLA alcançado (< 2s)? | — |

**Observações:**
> _Preencher após execução_

---

### 5.4 — Jobs Preservados Durante Falha

**SLA:** 0 jobs criados antes da falha podem ser perdidos.

| Campo | Valor |
|---|---|
| Jobs criados antes do caos | — |
| Jobs recuperáveis após restart | — |
| **Jobs perdidos** | **—** |
| SLA alcançado (0 perdidos)? | — |

**Observações:**
> _Preencher após execução_

---

## Ações de Remediação

> _Documentar qualquer ação necessária se SLA foi violado_

---

## Próxima Execução Planejada

Data: _preencher_  
Responsável: _preencher_
