# Auto Pre-Compaction Hook

## O que é

Script automático que monitora o uso de tokens das sessões OpenClaw e roda o `pre-compaction.py` automaticamente quando uma sessão ultrapassa 80% do limite.

## Por que foi criado

O OpenClaw compacta sessões automaticamente quando atingem o limite de tokens. O AGENTS.md define que antes de compactar, deve-se rodar `pre-compaction.py` para extrair memórias importantes. Porém, isso dependia do agente lembrar de fazer manualmente — risco alto de perda de informações importantes.

## Como funciona

1. **Monitoramento**: Script verifica todas as sessões via `openclaw sessions --json`
2. **Verificação**: Calcula porcentagem de uso de tokens (totalTokens / contextTokens)
3. **Disparo**: Se uso ≥ 80%, extrai transcript do arquivo `.jsonl` da sessão
4. **Extração**: Roda `pre-compaction.py` com o transcript para identificar memórias
5. **Salvamento**: Memórias são salvas no PostgreSQL via `upsert_memory()`

## Configuração

### Cron (automático)

```cron
*/5 * * * * cd /root/.openclaw/workspace && /usr/bin/python3 scripts/auto-pre-compact.py --once --threshold 80 >> /tmp/auto-pre-compact.log 2>&1
```

Roda a cada 5 minutos, verificando todas as sessões do agente `main`.

### Threshold

- **Default**: 80%
- **Configurável**: `--threshold <porcentagem>`
- **Racional**: 80% dá margem de segurança antes da compactação automática

## Uso manual

```bash
# Roda uma vez (para testes)
python3 scripts/auto-pre-compact.py --once --threshold 80

# Roda em modo contínuo (não recomendado com cron)
python3 scripts/auto-pre-compact.py --threshold 80 --interval 300

# Usar threshold diferente (ex: 70%)
python3 scripts/auto-pre-compact.py --once --threshold 70
```

## Logs

- **Log de execução**: `/tmp/auto-pre-compact.log`
- **Pre-compaction detalhes**: Logado no próprio script via `memory_sync_log` (PostgreSQL)

## Teste realizado

```bash
python3 scripts/auto-pre-compact.py --once --threshold 10
```

Resultado:
- 100 sessões verificadas
- 63 sessões acima do threshold
- Memórias extraídas e salvas com sucesso
- Apenas 3 erros (sessões sem arquivo .jsonl)

## Arquivos

- `scripts/auto-pre-compact.py` — Script principal
- `scripts/pre-compaction.py` — Script de extração de memórias (já existia)
- `/tmp/auto-pre-compact.log` — Log de execução

## Integração com memória existente

Coexiste com:
- `memory-sync-v2.py` — Sync de memórias a cada 15min (buffer diário)
- `pre-compaction.py` — Extração de memórias antes da compactação

**Fluxo de memória**:
1. Durante sessão → buffer diário via `memory-sync-v2.py`
2. Pré-compactação → extração via `auto-pre-compact.py`
3. Pós-compactação → `pre-compaction.py` (manual, se ainda necessário)

## Manutenção

- Verificar logs semanais: `tail -100 /tmp/auto-pre-compact.log`
- Se muitos erros, investigar arquivos `.jsonl` corrompidos ou faltantes
- Threshold pode ser ajustado conforme necessário

## Criado em

2026-05-11 — Subagent task: d9f15cbd-c28b-4bc7-8106-fa022a993ab8
