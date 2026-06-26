# linkedin-post-agent

Agent that analyzes recent POCs on GitHub and generates draft LinkedIn posts,
delivered as an Issue for human review (human-in-the-loop).

## Architecture

```
weekly cron (GitHub Actions)
  └─ src/main.py
       1. Discover repos with recent pushes         (plain code)
       2. Derive changed topic folders from commits  (plain code)
       3. Agent explores each topic subtree          (tool-use loop)
       4. Generate 3 post variations PT-BR + EN      (single call)
       5. Write drafts/ and open a review Issue      (gh CLI)
```

A "topic" is the first two path segments of a changed file
(`kubernetes/knative/service.yaml` → `kubernetes/knative`), so one monorepo
yields one post per changed area, not one giant post for the whole repo.
Root-level changes map to the repo itself.

## Setup

1. Create the secrets in the repository (Settings → Secrets and variables → Actions):
   - `ANTHROPIC_API_KEY` — Anthropic API key
   - `GH_POC_TOKEN` — (optional) fine-grained PAT with `contents: read`
     on the POC repos. Only needed for private repos.
2. Create the `linkedin-draft` label in the repo (used by the review issue).
3. Run manually: Actions → Generate LinkedIn Post Drafts → Run workflow.

## Local execution

```bash
export ANTHROPIC_API_KEY=...
export GH_POC_TOKEN=...        # optional — only for private repos (unauthenticated = public only, 60 req/hr)
export GH_USER=your-username
export DAYS_WINDOW=7
python src/main.py
```

Drafts are written to `drafts/latest.md`.

## Costs

~`MAX_POSTS` topics/week × (~`MAX_AGENT_TURNS` analysis tool turns + 1 writing call).
Tune `MAX_POSTS`, `MAX_AGENT_TURNS`, `MAX_COMMITS`, and the file truncation in
`src/main.py` to control token consumption. Each run prints actual token usage
and an estimated USD cost; set `PRICE_IN`/`PRICE_OUT` to match the active `MODEL`.
