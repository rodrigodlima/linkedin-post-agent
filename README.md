# linkedin-post-agent

Agent that analyzes recent POCs on GitHub and generates draft LinkedIn posts,
delivered as an Issue for human review (human-in-the-loop).

## Architecture

```
weekly cron (GitHub Actions)
  └─ src/main.py
       1. Discover repos with recent pushes        (plain code)
       2. Agent explores each repo via tools        (tool-use loop)
       3. Generate 3 post variations PT-BR + EN     (single call)
       4. Write drafts/ and open a review Issue     (gh CLI)
```

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
export GH_POC_TOKEN=...        # or any PAT with read access
export GH_USER=your-username
export DAYS_WINDOW=7
python src/main.py
```

Drafts are written to `drafts/latest.md`.

## Costs

~4 repos/week × (~10 analysis tool turns + 1 writing call).
Tune `MAX_REPOS`, `MAX_AGENT_TURNS`, and the file truncation in
`src/main.py` to control token consumption.
