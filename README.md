# linkedin-post-agent

Agent que analisa POCs recentes no GitHub e gera drafts de posts para o LinkedIn,
entregues como Issue para revisão humana (human-in-the-loop).

## Arquitetura

```
cron semanal (GitHub Actions)
  └─ src/main.py
       1. Descobre repos com push recente        (código puro)
       2. Agent explora cada repo via tools       (loop de tool use)
       3. Gera 3 variações de post PT-BR + EN     (chamada simples)
       4. Grava drafts/ e abre Issue de revisão   (gh CLI)
```

## Setup

1. Crie os secrets no repositório (Settings → Secrets and variables → Actions):
   - `ANTHROPIC_API_KEY` — chave da API da Anthropic
   - `GH_POC_TOKEN` — (opcional) PAT fine-grained com `contents: read`
     nos repos de POC. Necessário apenas para repos privados.
2. Crie a label `linkedin-draft` no repo (usada pela issue de revisão).
3. Rode manualmente: Actions → Generate LinkedIn Post Drafts → Run workflow.

## Execução local

```bash
export ANTHROPIC_API_KEY=...
export GH_POC_TOKEN=...        # ou um PAT qualquer com leitura
export GH_USER=seu-usuario
export DAYS_WINDOW=7
python src/main.py
```

Os drafts ficam em `drafts/latest.md`.

## Custos

~4 repos/semana × (~10 tool turns de análise + 1 chamada de redação).
Ajuste `MAX_REPOS`, `MAX_AGENT_TURNS` e o truncamento de arquivos em
`src/main.py` para controlar consumo de tokens.
