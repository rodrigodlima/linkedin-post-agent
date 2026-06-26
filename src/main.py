"""
linkedin-post-agent — analyzes recent POCs on GitHub and generates post drafts.

Pipeline:
  1. Discover repos with recent pushes (plain code, no LLM)
  2. Derive the topic folders changed in the window from recent commits
  3. For each topic: the agent explores only that subtree and summarizes it
  4. Generate post variations from the summary
  5. Write drafts/latest.md (the workflow opens the review issue)
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
MAX_AGENT_TURNS = 8
MAX_POSTS = 2  # 1-2 posts per week (cost-capped); one post per changed topic folder
MAX_COMMITS = 30  # cap per-commit detail fetches when deriving changed topics

PROMPTS = Path(__file__).parent.parent / "prompts"
DRAFTS = Path(__file__).parent.parent / "drafts"

client = anthropic.Anthropic(max_retries=4)

# ----------------------------------------------------------- cost tracking ---
# Prices per token. Default = Haiku 4.5 ($1/$5 per 1M in/out). Override via env
# to match the active MODEL (e.g. Sonnet 4.6 = 3/15). Cache read ~0.1x input,
# cache write ~1.25x input.
PRICE_IN = float(os.environ.get("PRICE_IN", "1.00")) / 1e6
PRICE_OUT = float(os.environ.get("PRICE_OUT", "5.00")) / 1e6
PRICE_CACHE_W = PRICE_IN * 1.25
PRICE_CACHE_R = PRICE_IN * 0.10

USAGE = {"input": 0, "output": 0, "cache_w": 0, "cache_r": 0}


def record(usage) -> None:
    USAGE["input"] += usage.input_tokens
    USAGE["output"] += usage.output_tokens
    USAGE["cache_w"] += usage.cache_creation_input_tokens or 0
    USAGE["cache_r"] += usage.cache_read_input_tokens or 0


def usd() -> float:
    return (
        USAGE["input"] * PRICE_IN
        + USAGE["output"] * PRICE_OUT
        + USAGE["cache_w"] * PRICE_CACHE_W
        + USAGE["cache_r"] * PRICE_CACHE_R
    )


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
    return out  # capped later, on the per-topic unit list


def topic_of(path: str) -> str:
    """Topic = first 2 directory segments of a changed file path.
    e.g. kubernetes/knative/service.yaml -> 'kubernetes/knative',
         terraform/main.tf               -> 'terraform',
         README.md                       -> '' (the whole repo)."""
    return "/".join(path.split("/")[:-1][:2])


def recent_topics(repo: str, days: int) -> list[str]:
    """Topic folders touched by commits in the window, newest first, deduped."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    commits = gh(f"/repos/{GH_USER}/{repo}/commits?since={cutoff}&per_page=100")
    topics: list[str] = []
    for c in commits[:MAX_COMMITS]:
        detail = gh(f"/repos/{GH_USER}/{repo}/commits/{c['sha']}")
        for f in detail.get("files", []):
            t = topic_of(f["filename"])
            if t not in topics:
                topics.append(t)
    return topics


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
        "description": "Reads the content of a repository file (truncated at 4000 chars).",
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


def execute_tool(name: str, inp: dict, topic: str = "") -> str:
    try:
        if name == "list_repo_tree":
            tree = gh(f"/repos/{GH_USER}/{inp['repo']}/git/trees/HEAD?recursive=1")
            paths = [i["path"] for i in tree["tree"]]
            if topic:  # scope the listing to the changed topic folder
                prefix = topic + "/"
                paths = [p for p in paths if p.startswith(prefix)]
            out = "\n".join(paths[:200])
            if tree.get("truncated"):
                out += "\n(tree truncated by GitHub: not all files listed)"
            return out or "(no files under this topic)"
        if name == "read_file":
            f = gh(f"/repos/{GH_USER}/{inp['repo']}/contents/{inp['path']}")
            if isinstance(f, list):
                # path is a directory; return the listing instead of breaking
                return "Directory, not a file. Contents:\n" + "\n".join(
                    i["path"] for i in f
                )
            content = base64.b64decode(f["content"]).decode("utf-8", errors="replace")
            return content[:4000]
        if name == "get_recent_commits":
            commits = gh(f"/repos/{GH_USER}/{inp['repo']}/commits?per_page=10")
            return "\n".join(f"- {c['commit']['message'].splitlines()[0]}" for c in commits)
        return f"Unknown tool: {name}"
    except Exception as e:
        # Returning the error to the model lets it recover (e.g. wrong path)
        return f"Error executing {name}: {e}"


# --------------------------------------------------------- step 2: analysis ---

def analyze_topic(repo_name: str, topic: str) -> str:
    prompt = (PROMPTS / "analyze.md").read_text(encoding="utf-8")
    topic_label = topic or "(the repository root — analyze the whole repo)"
    messages = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": prompt.format(repo=repo_name, topic=topic_label),
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
        record(resp.usage)
        if resp.stop_reason != "tool_use":
            return next((b.text for b in resp.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": resp.content})
        results = [
            {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": execute_tool(b.name, b.input, topic),
            }
            for b in resp.content
            if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})

    return "(analysis interrupted: iteration limit reached)"


# ---------------------------------------------------------- step 3: writing ---

def write_posts(repo_name: str, branch: str, topic: str, summary: str) -> str:
    prompt = (PROMPTS / "write_post.md").read_text(encoding="utf-8")
    if topic:
        subject = f"{repo_name}/{topic}"
        repo_url = f"github.com/{GH_USER}/{repo_name}/tree/{branch}/{topic}"
    else:
        subject = repo_name
        repo_url = f"github.com/{GH_USER}/{repo_name}"
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": prompt.format(repo=subject, summary=summary, repo_url=repo_url),
        }],
    )
    record(resp.usage)
    return next((b.text for b in resp.content if b.type == "text"), "")


# ------------------------------------------------------------------- main ---

def main() -> int:
    if TARGET_REPO:
        repos = [gh(f"/repos/{GH_USER}/{TARGET_REPO}")]
    else:
        repos = recent_repos(DAYS_WINDOW)

    if not repos:
        print(f"No repos with activity in the last {DAYS_WINDOW} days. Nothing to do.")
        return 0  # no work is not a failure; the workflow checks drafts/latest.md

    # One unit = one (repo, branch, topic-folder) changed in the window. Newest
    # repos first; cap the total number of posts.
    units = []
    for repo in repos:
        name = repo["name"]
        branch = repo.get("default_branch", "main")
        for topic in recent_topics(name, DAYS_WINDOW):
            units.append((name, branch, topic))
            if len(units) >= MAX_POSTS:
                break
        if len(units) >= MAX_POSTS:
            break

    if not units:
        print(f"No file changes in the last {DAYS_WINDOW} days. Nothing to do.")
        return 0

    print(f"Selected topics: {[f'{n}/{t}' if t else n for n, _, t in units]}")

    DRAFTS.mkdir(exist_ok=True)
    sections = []
    summaries = {}

    for name, branch, topic in units:
        before = usd()
        label = f"{name}/{topic}" if topic else name
        print(f"→ Analyzing {label}...")
        summary = analyze_topic(name, topic)
        summaries[label] = summary

        print(f"→ Generating drafts for {label}...")
        posts = write_posts(name, branch, topic, summary)
        sections.append(f"## 📦 {label}\n\n{posts}\n\n---\n")
        print(f"  {label}: ${usd() - before:.4f}")

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
    print(f"Drafts written to drafts/latest.md ({len(units)} posts).")
    print(
        f"Tokens — in: {USAGE['input']:,} out: {USAGE['output']:,} "
        f"cache_w: {USAGE['cache_w']:,} cache_r: {USAGE['cache_r']:,}"
    )
    print(f"Estimated cost ({MODEL}): ${usd():.4f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Execution failed: {e}", file=sys.stderr)
        sys.exit(1)
