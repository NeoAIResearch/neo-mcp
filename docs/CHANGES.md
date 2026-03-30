# Neo MCP — Pending Backend Change

One backend change needed to complete the zero-friction setup experience.

---

## The problem

When `neo-mcp daemon` runs with only `NEO_SECRET_KEY` set (no OAuth login), it polls the execution command endpoint:

```
GET <daemon-poll-endpoint>/<deployment_id>
Authorization: Bearer sk-v1-a63a...
```

The backend returns 401. The daemon cannot receive execution commands. Tasks submitted with this deployment_id time out.

**This is the only thing blocking fully zero-touch setup.**

---

## What the daemon poll endpoint does

The backend delivers execution commands through the poll endpoint. When a user submits a task:

1. `POST /v2/thread/init-chat-direct` — routes task to `deployment_id` ✅ works with API key
2. Backend queues commands for the daemon (run this script, write this file, etc.)
3. Daemon picks up commands via `GET <poll-endpoint>/<deployment_id>` ❌ 401 with API key
4. Daemon executes and replies via `POST <poll-response-endpoint>` ❌ 401 with API key

Steps 1, status polling (`/v2/thread/status`), and message retrieval (`/v2/thread/thread-messages`) all work with the API key today. The daemon poll endpoint is the only exception.

> **Note:** The exact endpoint path is TBD — likely `v2/thread/poll` or similar. Update this doc when confirmed.

---

## The fix

```python
# Today
if not is_valid_oauth_token(bearer_token):
    return HTTP_401("Unauthorized")

# After change
if not is_valid_oauth_token(bearer_token) and not is_valid_api_key(bearer_token):
    return HTTP_401("Unauthorized")
```

Two endpoints need this change:
- `GET <daemon-poll-endpoint>/{deployment_id}`
- `POST <daemon-poll-response-endpoint>`

---

## Why it's safe

The deployment UUID is derived from the API key:

```
UUID = SHA-256(NEO_SECRET_KEY)[:16]  →  formatted as UUID
```

Knowing the UUID proves you hold the API key. It's 128-bit, not guessable, and deterministic — same key always produces the same UUID. The daemon and the hosted MCP server independently compute the same UUID from the same key, so routing works automatically.

---

## Final UX once this ships

```bash
# One command. That's it.
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."

# Submit a task — agent auto-starts daemon, daemon polls with API key, everything works.
```

No `neo-mcp login`. No `neo-mcp daemon`. No deployment ID header. No OAuth.

---

## Current state

| Workflow | Works today? |
|---|---|
| VS Code/Cursor extension | ✅ Extension polls with its own OAuth token |
| `pip install` + `neo-mcp login` | ✅ Login auto-starts daemon; daemon has OAuth token |
| Agent with terminal (Claude Code, Cursor, Codex CLI, Windsurf) | ✅ Agent runs `neo-mcp daemon &` on first task (user approves) — still needs login for OAuth |
| `pip install` + API key only + `neo-mcp daemon` (no login) | ❌ 401 on poll endpoint |
| Headless / SSH server (no browser) | ❌ OAuth browser callback cannot fire |

After the poll endpoint change: **all rows become ✅** and `neo-mcp login` is fully optional.
