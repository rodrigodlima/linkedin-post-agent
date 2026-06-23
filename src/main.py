"""
linkedin-post-agent — analyzes recent POCs on GitHub and generates post drafts.

Pipeline:
  1. Discover repos with recent pushes (plain code, no LLM)
  2. For each repo: the agent explores via tools and produces a technical summary
  3. Generate post variations from the summary
  4. Write drafts/latest.md (the workflow opens the review issue)
"""

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

# ---------------------------------------------------------------- config ---

GH_TOKEN = os.environ.get("GH_POC_TOKEN", "").strip()  # optional: only needed for private repos
GH_USER = os.environ["GH_USER"]
DAYS_WINDOW = int(os.environ.get("DAYS_WINDOW", "7"))
TARGET_REPO = os.environ.get("TARGET_REPO", "").strip()
MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")
MAX_AGENT_TURNS = 15
MAX_REPOS = 4  # 3-4 posts per week

PROMPTS = Path(__file__).parent.parent / "prompts"
DRAFTS = Path(__file__).parent.parent / "drafts"

client = anthropic.Anthropic(max_retries=4)

# ---------------------------------------------------------- github client ---

session = requests.Session()
session.headers.update({"Accept": "application/vnd.github+json"})
if GH_TOKEN:
    session.headers["Authorization"] = f"Bearer {GH_TOKEN}"


def gh(path: str):
    r = session.get(f"https://api.github.com{path}", timeout=30)
    r.raise_for_status()
    return r.json()


def recent_repos(days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    repos = gh(f"/users/{GH_USER}/repos?sort=pushed&per_page=20&type=owner")
    out = []
    for repo in repos:
        pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
        if pushed >= cutoff and not repo["fork"]:
            out.append(repo)
    return out[:MAX_REPOS]


# ------------------------------------------------------------ agent tools ---

TOOLS = [
    {
        "name": "list_repo_tree",
        "description": "Lists the repository file paths (up to 200 items).",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
    {
        "name": "read_file",
        "description": "Reads the content of a repository file (truncated at 8000 chars).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "get_recent_commits",
        "description": "Returns the 10 most recent commit messages of the repository.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
        # Cache breakpoint: tool definitions are resent on every loop turn
        # (up to MAX_AGENT_TURNS), so caching them cuts tokens.
        "cache_control": {"type": "ephemeral"},
    },
]


def execute_tool(name: str, inp: dict) -> str:
    try:
        if name == "list_repo_tree":
            tree = gh(f"/repos/{GH_USER}/{inp['repo']}/git/trees/HEAD?recursive=1")
            paths = "\n".join(i["path"] for i in tree["tree"][:200])
            if tree.get("truncated"):
                paths += "\n(tree truncated by GitHub: not all files listed)"
            return paths
        if name == "read_file":
            f = gh(f"/repos/{GH_USER}/{inp['repo']}/contents/{inp['path']}")
            if isinstance(f, list):
                # path is a directory; return the listing instead of breaking
                return "Directory, not a file. Contents:\n" + "\n".join(
                    i["path"] for i in f
                )
            content = base64.b64decode(f["content"]).decode("utf-8", errors="replace")
            return content[:8000]
        if name == "get_recent_commits":
            commits = gh(f"/repos/{GH_USER}/{inp['repo']}/commits?per_page=10")
            return "\n".join(f"- {c['commit']['message'].splitlines()[0]}" for c in commits)
        return f"Unknown tool: {name}"
    except Exception as e:
        # Returning the error to the model lets it recover (e.g. wrong path)
        return f"Error executing {name}: {e}"


# --------------------------------------------------------- step 2: analysis ---

def analyze_repo(repo_name: str) -> str:
    prompt = (PROMPTS / "analyze.md").read_text(encoding="utf-8")
    messages = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": prompt.format(repo=repo_name),
            # Stable prefix during the loop; caches the base instruction.
            "cache_control": {"type": "ephemeral"},
        }],
    }]

    for _ in range(MAX_AGENT_TURNS):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=3000,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return next((b.text for b in resp.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": resp.content})
        results = [
            {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": execute_tool(b.name, b.input),
            }
            for b in resp.content
            if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})

    return "(analysis interrupted: iteration limit reached)"


# ---------------------------------------------------------- step 3: writing ---

def write_posts(repo_name: str, summary: str) -> str:
    prompt = (PROMPTS / "write_post.md").read_text(encoding="utf-8")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": prompt.format(repo=repo_name, summary=summary, gh_user=GH_USER),
        }],
    )
    return next((b.text for b in resp.content if b.type == "text"), "")


# ------------------------------------------------------------------- main ---

def main() -> int:
    if TARGET_REPO:
        repos = [{"name": TARGET_REPO}]
    else:
        repos = recent_repos(DAYS_WINDOW)

    if not repos:
        print(f"No repos with activity in the last {DAYS_WINDOW} days. Nothing to do.")
        return 0  # no work is not a failure; the workflow checks drafts/latest.md

    print(f"Selected repos: {[r['name'] for r in repos]}")

    DRAFTS.mkdir(exist_ok=True)
    sections = []
    summaries = {}

    for repo in repos:
        name = repo["name"]
        print(f"→ Analyzing {name}...")
        summary = analyze_repo(name)
        summaries[name] = summary

        print(f"→ Generating drafts for {name}...")
        posts = write_posts(name, summary)
        sections.append(f"## 📦 {name}\n\n{posts}\n\n---\n")

    today = datetime.now().strftime("%Y-%m-%d")
    body = (
        f"Drafts generated on {today} (window: {DAYS_WINDOW} days).\n\n"
        "Review, edit and post manually. Close the issue when done.\n\n---\n\n"
        + "\n".join(sections)
    )

    (DRAFTS / "latest.md").write_text(body, encoding="utf-8")
    (DRAFTS / f"{today}.md").write_text(body, encoding="utf-8")
    (DRAFTS / f"{today}-summaries.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Drafts written to drafts/latest.md ({len(repos)} repos).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Execution failed: {e}", file=sys.stderr)
        sys.exit(1)
