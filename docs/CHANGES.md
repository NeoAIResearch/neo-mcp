# Neo MCP — Auth

All Neo MCP authentication uses a single credential: `NEO_SECRET_KEY` (`sk-v1-...`).

The same API key is used for:
- Task submission (`POST /v2/thread/init-chat-direct`)
- Status polling (`GET /v2/thread/status/{thread_id}`)
- Message retrieval (`GET /v2/thread/thread-messages`)
- Daemon poll (`GET /v2/poll/{deployment_id}`)
- Daemon response (`POST /v2/poll/response`)

No OAuth. No `neo-mcp login`. No browser flow for the daemon.

---

## Setup

```bash
# One command. That's it.
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."
```

Submit a task → agent auto-starts daemon → daemon polls with API key → everything works.

---

## Deployment UUID

The deployment UUID is derived deterministically from the API key:

```
UUID = SHA-256(NEO_SECRET_KEY)[:16]  →  formatted as UUID
```

The daemon and the hosted MCP server independently compute the same UUID from the same key — no coordination or config files needed.

---

## Workflow support

| Workflow | Status |
|---|---|
| VS Code/Cursor extension | ✅ |
| `pip install` + API key only | ✅ Daemon auto-starts on first task |
| Agent with terminal (Claude Code, Cursor, Codex CLI, Windsurf) | ✅ Agent starts daemon on first task (user approves) |
| Headless / SSH server | ✅ `NEO_SECRET_KEY` + `neo-mcp daemon &` |
| Hosted server only (no daemon) | ✅ Status/messages work; local file writes require daemon |
