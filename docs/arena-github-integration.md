# Integrating an Arena Agent with GitHub

**Status:** last checked 2026-10-14 from this workspace — package metadata and imports (`mcp-arena` 0.4.1), GitHub repository API, and Arena's published documentation. Anything not re-checkable from here is phrased conservatively; see [Notes on this draft](#notes-on-this-draft).
**Scope:** two genuinely different paths — the **native Agent Mode connector** (recommended) and a **custom MCP-based pipeline** via `mcp-arena`.

---

## 🚀 Path 1: Native Agent Mode (recommended)

1. **Connect GitHub** — In Agent Mode, open the connector menu, select GitHub, sign in via OAuth, and grant access to the organizations/repositories you want ([official announcement](https://arena.ai/blog/coding-in-agent-mode)). There is also a dedicated GitHub App: [Arena AI Coding Agent](https://github.com/apps/arena-ai-coding-agent).
2. **Start a session on a repo** — pick a repo and branch; Arena clones it into a **sandbox at session start** ([official announcement](https://arena.ai/blog/coding-in-agent-mode)).
3. **Prompt the agent** — it reads the codebase, edits files, and runs commands; every change appears live in the **diff panel**, toggleable between *"last turn"* and *"full branch"*, syntax-highlighted ([official announcement](https://arena.ai/blog/coding-in-agent-mode)).
4. **Review and ship** — the agent commits to a working branch, pushes, and opens a PR; you review in the Diff tab and merge ([help article](https://help.arena.ai/articles/5432423882-how-to-use-agent-mode)).

**Limitations to state honestly:**

- Only repositories you authorized through the GitHub OAuth connector are in scope.
- The repository must already exist and be reachable before the session starts.
- Once a PR is merged or closed, that session can no longer push to GitHub; files created afterwards stay in the session only ([help article](https://help.arena.ai/articles/5432423882-how-to-use-agent-mode)).

---

## ⚙️ Path 2: Custom integration with `mcp-arena`

```bash
pip install "mcp-arena[github,agents]"   # github preset + LangChain bridge; quote it!
```

```python
import os
# yes: "presents" — the literal directory name in the published package, not a typo here
from mcp_arena.presents.github import GithubMCPServer
from mcp_arena.agent import make_mcp_agent

server = GithubMCPServer(token=os.environ["GITHUB_TOKEN"])  # or no-arg: reads $GITHUB_TOKEN
# ... then pass this server to make_mcp_agent(llm, [server], ...)
```

**Notes:**

- Requires **Python 3.12+** (package metadata: `Requires-Python >= 3.12`); the package is Alpha-status on PyPI.
- The `github` extra installs only the PyGithub-backed preset (`pygithub>=2.8,<3`). The **`agents` extra is what makes `make_mcp_agent` importable** (it pulls in `langchain` + `langchain-mcp-adapters`) — installing `[github]` alone fails at import with `ModuleNotFoundError: No module named 'langchain_core'`.
- Always **quote the pip spec** (`"mcp-arena[github]"`) so zsh does not glob the brackets.
- The package directory is **literally named `presents`** — that is the published package's own spelling, **not a typo in this document**, and there is no `mcp_arena/presets/`. Do not "fix" it to `presets`: the import fails with `ModuleNotFoundError: No module named 'mcp_arena.presets'`. Both imports above were exercised against the 0.4.1 wheel.
- **Check caveat (kept visible on purpose):** the import probes behind these notes ran under Python 3.11.2 in a sandbox without 3.12, installed with `pip --ignore-requires-python`. That establishes which module paths exist in the 0.4.1 wheel; it does not claim 3.11 runtime support. The package's own requirement stands: Python 3.12+.

---

## 📚 Resources

| Resource | Description | Link |
|:--|:--|:--|
| Arena blog: Coding in Agent Mode | Primary source for the native workflow | [arena.ai/blog/coding-in-agent-mode](https://arena.ai/blog/coding-in-agent-mode) |
| Arena help: How to use Agent Mode | Covers repo connection, Diff tab, PR merge behavior | [help.arena.ai](https://help.arena.ai/articles/5432423882-how-to-use-agent-mode) |
| Arena product changelog | Arena's product changelog documents GitHub support in Agent Mode. | [arena.ai/company/product-changelog](https://arena.ai/company/product-changelog) |
| mcp_arena | MCP server presets + `make_mcp_agent` bridge | [GitHub](https://github.com/SatyamSingh8306/mcp_arena) · [PyPI](https://pypi.org/project/mcp-arena/) |
| gabebrain-skills | Domain-specific skills repository (geology/GIS-heavy) that integrates with Arena AI through a bridge — a working example of hybrid architecture, not a general-purpose starter. | [GitHub](https://github.com/gabrielhklaser/gabebrain-skills) |

---

## Notes on this draft

Phrased as differences from the original pasted text; each line reflects command output from this workspace on 2026-10-14.

1. **Import path — same as the original pasted text.** A later review flagged `mcp_arena.presents.github` as a typo in favor of `presets`; the check shows the opposite: `from mcp_arena.presets.github import …` raises `ModuleNotFoundError: No module named 'mcp_arena.presets'`, while `from mcp_arena.presents.github import GithubMCPServer` succeeds (with the `github` extra installed).
2. **Install command — differs from the original pasted text.** The original installed only `mcp-arena[github]`, which does not provide `make_mcp_agent`; wheel metadata lists `Provides-Extra: agents` → `langchain`, `langchain-mcp-adapters`, and the import fails without them. The command here adds the `agents` extra.
3. **Shell quoting — differs from the original pasted text.** The pip spec is quoted so zsh does not glob `[...]`.
4. **Resource rows — differ from the original pasted text.** The Agent-Gaunlet-Starter-Kit row was dropped entirely (the reasoning is preserved in this repo's commit history rather than asserted here about a third-party project). The gabebrain-skills row was kept after checking that the repository resolves and its README describes a domain-specific skills set integrated with Arena AI; the wording is a characterization, not a claim of affiliation.
5. **Help-center link — differs from the original pasted text.** The original's `help.arena.ai/articles/6934758149-…` URL could not be re-confirmed from here; only links that resolved are listed above.
