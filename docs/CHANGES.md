# Neo MCP — Required Backend Changes

Two backend changes would eliminate all setup friction for users. Everything on the MCP client side is already built and working. These are the only remaining blockers.

---

## Change 1 — Cloud execution fallback (highest impact)

### The problem

Every task submission requires a `deployment_id` pointing to an active daemon process. If no daemon is running, the backend returns:

```
HTTP 400: No healthy deployments available
```

This forces every user to run a local process before they can use Neo at all — either the VS Code extension or `neo-mcp daemon`. New users hit this wall immediately.

### What needs to change

Add a cloud execution fallback in the `init-chat-direct` routing layer:

```python
# Today
if not get_healthy_deployment(deployment_id):
    return HTTP_400("No healthy deployments available")

# After change
if not get_healthy_deployment(deployment_id):
    cloud_container = allocate_cloud_sandbox()
    route_to_cloud(thread_id, cloud_container)
    return HTTP_200({"thread_id": thread_id})
```

When no active daemon is registered for the given `deployment_id` (or no `deployment_id` is provided at all), the backend spins up a cloud container and runs the task there instead of rejecting it.

### What stays the same

The MCP server doesn't change. `neo_submit_task` → `neo_task_status` → `neo_get_messages` / `neo_get_files` — same API, same tools, same flow. The backend handles execution transparently.

Users with the VS Code extension or `neo-mcp daemon` running continue to get local execution (files write to their machine). Users without either get cloud execution automatically — no change needed on their end.

### File output difference

| Execution path | Where files land |
|---|---|
| Local daemon (extension or `neo-mcp daemon`) | User's filesystem (`~/project/model.py`) |
| Cloud fallback | Neo cloud storage → retrievable via `neo_get_files` |

For most ML tasks (train a model, analyze data, generate code), files via `neo_get_files` is fine. Local execution is only required when the task must read/write the user's actual filesystem in real time.

### User experience after this change

```bash
# One command. No extension, no pip, no daemon, no UUID.
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."

# Submit tasks. They just run.
```

This is the same UX as Linear, Stripe, and Sentry MCP servers — one command, works immediately.

---

## Change 2 — Accept API key on `/v2/poll` (eliminates OAuth login)

### The problem

The daemon polls `/v2/poll/{deployment_id}` to receive task execution commands. This endpoint currently requires an OAuth token — it rejects the `NEO_SECRET_KEY` API key with 401.

This forces users who run `neo-mcp daemon` manually (without the VS Code extension) to go through a browser OAuth flow (`neo-mcp login`) before the daemon works. On remote servers and headless environments, this is broken — the localhost OAuth callback never fires.

### What the daemon already does

The Python daemon already sends `NEO_SECRET_KEY` as a Bearer token fallback when no OAuth token is present. The only reason it fails is the backend rejects it.

```
# What the daemon sends today (when no OAuth token):
GET /v2/poll/de9d7297-580c-587c-b0e4-7ebb0fe7314c
Authorization: Bearer sk-v1-a63a...   ← backend returns 401

# What needs to work:
GET /v2/poll/de9d7297-580c-587c-b0e4-7ebb0fe7314c
Authorization: Bearer sk-v1-a63a...   ← backend returns 200
```

### Why this is safe

The deployment UUID is derived deterministically from the API key:

```
UUID = SHA-256(NEO_SECRET_KEY)[:16]  →  formatted as UUID
```

Properties:
- 128-bit — not guessable by brute force
- Derived from a private API key — only the key holder can compute it
- Possession of the UUID = proof of API key ownership

Accepting the API key on the poll endpoint adds no security risk — it's equivalent to what OAuth provides, just without the browser ceremony.

Both the hosted MCP server and the user's local daemon independently compute the same UUID from the same API key. This is already implemented and tested on the client side.

### What needs to change

```python
# Today — poll endpoint
if not is_valid_oauth_token(bearer_token):
    return HTTP_401("Unauthorized")

# After change — also accept API key
if not is_valid_oauth_token(bearer_token) and not is_valid_api_key(bearer_token):
    return HTTP_401("Unauthorized")
```

Endpoints to update:
- `GET /v2/poll/{deployment_id}` — receive task commands
- `POST /v2/poll/response` — send execution results back

### User experience after this change

```bash
# Start daemon. No browser, no login, works on SSH/headless.
NEO_SECRET_KEY=sk-v1-... neo-mcp daemon &

# Add MCP. No X-Neo-Deployment-Id header needed.
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."
```

`neo-mcp login` becomes entirely optional — useful for users who want OAuth-based auth with automatic token refresh, but never required.

---

## Current state without these changes

| User setup | Works today? | What fails |
|---|---|---|
| VS Code/Cursor extension | Yes | Nothing — extension handles everything |
| `pip install` + API key (local stdio) | Yes | Daemon auto-starts, extension UUID used via registration |
| Hosted server + extension UUID header | Yes | User must manually find and paste extension UUID |
| Hosted server, no extension, no pip | **No** | 400 on submit — no daemon registered (Change 1 fixes this) |
| `neo-mcp daemon` without OAuth login | **No** | 401 on poll — API key rejected (Change 2 fixes this) |
| Headless server / SSH | **No** | OAuth callback never fires (Change 2 fixes this) |

## After both changes

Every user experience works with just an API key. OAuth login and local daemons become optional enhancements, not requirements.
