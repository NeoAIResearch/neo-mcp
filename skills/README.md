# Neo Skills & Framework Integrations

---

## Claude Code Slash Commands

These are installed into `~/.claude/skills/` and become `/slash-commands` in Claude Code.

| Folder | Command | What it does |
|---|---|---|
| [`claude-code/`](claude-code/SKILL.md) | `/neo` | Auto-routes AI/ML tasks to Neo, manages full lifecycle. Also exposes `neo_add_integration` / `neo_list_integrations` / `neo_test_integration` / `neo_remove_integration` for registering GitHub / HuggingFace / Anthropic / OpenRouter keys locally — see [docs/INTEGRATIONS.md](../docs/INTEGRATIONS.md). |
| [`neo-setup/`](neo-setup/SKILL.md) | `/neo-setup` | Interactive installation and configuration wizard |

**Install automatically** (npm):
```bash
npm install -g neo-mcp   # copies skills to ~/.claude/skills/ automatically
```

**Install manually** (pip or manual update):
```bash
curl -fsSL https://raw.githubusercontent.com/heyneo/neo-mcp/main/skills/claude-code/SKILL.md \
  -o ~/.claude/skills/neo.md
```

---

## Framework Integrations (For Developers)

Copy-paste code for building apps and agents that use Neo as an ML backend. Each guide has two options: an MCP client (zero boilerplate) and inline tool definitions (no MCP dependency).

All integrations hit: `https://mcpserver.heyneo.com/mcp` with `Authorization: Bearer sk-v1-YOUR_KEY`

| Folder | Framework | Install |
|---|---|---|
| [`vercel/`](vercel/SKILL.md) | Vercel AI SDK — Next.js, serverless | `npm install ai @modelcontextprotocol/sdk` |
| [`langchain/`](langchain/SKILL.md) | LangChain / LangGraph | `pip install langchain-mcp-adapters langgraph` |
| [`openai-agents/`](openai-agents/SKILL.md) | OpenAI Agents SDK | `pip install openai-agents` |

---

Get your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

**Full authoring guide:** [`docs/SKILLS.md`](../docs/SKILLS.md)
