"""
linkedin-post-agent — analisa POCs recentes no GitHub e gera drafts de posts.

Pipeline:
  1. Descobrir repos com push recente (código puro, sem LLM)
  2. Para cada repo: agent explora via tools e produz resumo técnico
  3. Gerar variações de post a partir do resumo
  4. Gravar drafts/latest.md (o workflow cria a issue de revisão)
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

GH_TOKEN = os.environ["GH_POC_TOKEN"]
GH_USER = os.environ["GH_USER"]
DAYS_WINDOW = int(os.environ.get("DAYS_WINDOW", "7"))
TARGET_REPO = os.environ.get("TARGET_REPO", "").strip()
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
MAX_AGENT_TURNS = 15
MAX_REPOS = 4  # 3-4 posts por semana

PROMPTS = Path(__file__).parent.parent / "prompts"
DRAFTS = Path(__file__).parent.parent / "drafts"

client = anthropic.Anthropic(max_retries=4)

# ---------------------------------------------------------- github client ---

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
})


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
        "description": "Lista os caminhos de arquivos do repositório (até 200 itens).",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
    {
        "name": "read_file",
        "description": "Lê o conteúdo de um arquivo do repositório (truncado em 8000 chars).",
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
        "description": "Retorna as 10 mensagens de commit mais recentes do repositório.",
        "input_schema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
        # Breakpoint de cache: as definições de tools são reenviadas a cada
        # turn do loop (até MAX_AGENT_TURNS), então cacheá-las corta tokens.
        "cache_control": {"type": "ephemeral"},
    },
]


def execute_tool(name: str, inp: dict) -> str:
    try:
        if name == "list_repo_tree":
            tree = gh(f"/repos/{GH_USER}/{inp['repo']}/git/trees/HEAD?recursive=1")
            paths = "\n".join(i["path"] for i in tree["tree"][:200])
            if tree.get("truncated"):
                paths += "\n(árvore truncada pelo GitHub: nem todos os arquivos listados)"
            return paths
        if name == "read_file":
            f = gh(f"/repos/{GH_USER}/{inp['repo']}/contents/{inp['path']}")
            if isinstance(f, list):
                # path é um diretório; devolve a listagem em vez de quebrar
                return "Diretório, não arquivo. Conteúdo:\n" + "\n".join(
                    i["path"] for i in f
                )
            content = base64.b64decode(f["content"]).decode("utf-8", errors="replace")
            return content[:8000]
        if name == "get_recent_commits":
            commits = gh(f"/repos/{GH_USER}/{inp['repo']}/commits?per_page=10")
            return "\n".join(f"- {c['commit']['message'].splitlines()[0]}" for c in commits)
        return f"Tool desconhecida: {name}"
    except Exception as e:
        # Devolver o erro ao modelo permite que ele se recupere (ex.: path errado)
        return f"Erro ao executar {name}: {e}"


# -------------------------------------------------------- etapa 2: análise ---

def analyze_repo(repo_name: str) -> str:
    prompt = (PROMPTS / "analyze.md").read_text(encoding="utf-8")
    messages = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": prompt.format(repo=repo_name),
            # Prefixo estável durante o loop; cacheia a instrução base.
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

    return "(análise interrompida: limite de iterações atingido)"


# -------------------------------------------------------- etapa 3: redação ---

def write_posts(repo_name: str, summary: str) -> str:
    prompt = (PROMPTS / "write_post.md").read_text(encoding="utf-8")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": prompt.format(repo=repo_name, summary=summary),
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
        print(f"Nenhum repo com atividade nos últimos {DAYS_WINDOW} dias. Nada a fazer.")
        return 0  # sem trabalho não é falha; o workflow checa drafts/latest.md

    print(f"Repos selecionados: {[r['name'] for r in repos]}")

    DRAFTS.mkdir(exist_ok=True)
    sections = []
    summaries = {}

    for repo in repos:
        name = repo["name"]
        print(f"→ Analisando {name}...")
        summary = analyze_repo(name)
        summaries[name] = summary

        print(f"→ Gerando drafts para {name}...")
        posts = write_posts(name, summary)
        sections.append(f"## 📦 {name}\n\n{posts}\n\n---\n")

    today = datetime.now().strftime("%Y-%m-%d")
    body = (
        f"Drafts gerados em {today} (janela: {DAYS_WINDOW} dias).\n\n"
        "Revise, edite e poste manualmente. Feche a issue quando terminar.\n\n---\n\n"
        + "\n".join(sections)
    )

    (DRAFTS / "latest.md").write_text(body, encoding="utf-8")
    (DRAFTS / f"{today}.md").write_text(body, encoding="utf-8")
    (DRAFTS / f"{today}-summaries.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Drafts gravados em drafts/latest.md ({len(repos)} repos).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Falha na execução: {e}", file=sys.stderr)
        sys.exit(1)
