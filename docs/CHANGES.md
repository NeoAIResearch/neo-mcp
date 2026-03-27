# Neo MCP — Required Backend Change

One backend change: accept the API key on `/v2/poll`.

---

## The problem

When `neo-mcp daemon` runs with only `NEO_SECRET_KEY` set (no OAuth login), it sends:

```
GET /v2/poll/de9d7297-580c-587c-b0e4-7ebb0fe7314c
Authorization: Bearer sk-v1-a63a...
```

The backend returns 401. The daemon cannot receive execution commands. Tasks submitted with this deployment_id time out.

**This is the only thing blocking the no-extension workflow.**

---

## What `/v2/poll` does

The backend delivers execution commands through `/v2/poll/{deployment_id}`. When a user submits a task:

1. `POST /v2/thread/init-chat-direct` — routes task to `deployment_id` ✅ works with API key
2. Backend queues commands for the daemon (run this script, write this file, etc.)
3. Daemon picks up commands via `GET /v2/poll/{deployment_id}` ❌ 401 with API key
4. Daemon executes and replies via `POST /v2/poll/response` ❌ 401 with API key

Steps 1, status polling (`/v2/thread/status`), and message retrieval (`/v2/thread/thread-messages`) all work with the API key today. The poll endpoint is the only exception.

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

Two endpoints:
- `GET /v2/poll/{deployment_id}`
- `POST /v2/poll/response`

---

## Why it's safe

The deployment UUID is derived from the API key:

```
UUID = SHA-256(NEO_SECRET_KEY)[:16]  →  formatted as UUID
```

Knowing the UUID proves you hold the API key. It's 128-bit, not guessable, and deterministic — same key always produces the same UUID. The daemon and the hosted MCP server independently compute the same UUID from the same key, so routing works automatically.

---

## What works after this change

```bash
# Terminal 1: start daemon with just the API key — no login, no OAuth
NEO_SECRET_KEY=sk-v1-... neo-mcp daemon &

# Terminal 2: add MCP server — no X-Neo-Deployment-Id header needed
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."

# Done. Submit tasks. They execute locally via the daemon.
```

---

## Current state

| Workflow | Works today? |
|---|---|
| VS Code/Cursor extension | ✅ Extension polls with its own OAuth token |
| `pip install` + `neo-mcp login` + `neo-mcp daemon` | ✅ Daemon has OAuth token |
| `pip install` + `NEO_SECRET_KEY` + `neo-mcp daemon` (no login) | ❌ 401 on `/v2/poll` |
| Headless / SSH server | ❌ OAuth browser callback never fires |

After the change: the bottom two rows become ✅.
