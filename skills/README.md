# Neo Skills & Agent Integrations

Each folder contains a self-contained integration guide for a specific agent framework or AI editor.

| Folder | Framework | Auth | Transport |
|---|---|---|---|
| [`claude-code/`](claude-code/SKILL.md) | Claude Code (`/neo` slash command) | `NEO_SECRET_KEY` | stdio or HTTP |
| [`neo-setup/`](neo-setup/SKILL.md) | Setup wizard (`/neo-setup`) — auth, daemon, editor config | `NEO_SECRET_KEY` | — |
| [`vercel/`](vercel/SKILL.md) | Vercel AI SDK (Next.js, serverless) | `NEO_SECRET_KEY` | HTTP |
| [`openai-agents/`](openai-agents/SKILL.md) | OpenAI Agents SDK | `NEO_SECRET_KEY` | HTTP or stdio |
| [`langchain/`](langchain/SKILL.md) | LangChain / LangGraph | `NEO_SECRET_KEY` | HTTP or stdio |

All integrations connect to the same hosted MCP server: `https://mcpserver.heyneo.com/mcp`

Get your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.
