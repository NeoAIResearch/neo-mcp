# Neo MCP — Authentication & Setup: What Was Built and What Still Needs Backend Changes

## Current State (as of v0.2.4)

The MCP client side is fully implemented. Here's what works today, what requires backend changes,
and the exact steps to use the package.

---

## Quickstart: pip install + API key only

```bash
# 1. Install
pip install neo-mcp

# 2. Set your key
export NEO_SECRET_KEY=sk-v1-your-key

# 3. Register with Claude Code (stdio mode — runs on your machine)
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=your-key \
  -- neo-mcp

# 4. Submit tasks — no login, no extra setup
# Claude Code → neo_submit_task → MCP server auto-starts daemon → task executes locally
```

**That's it.** No `neo-mcp login`, no `neo-mcp daemon` pre-start, no browser OAuth.

### How it works under the hood

```
Claude Code
    │
    │  neo_submit_task("train a classifier")
    ▼
MCP server (neo-mcp process on your machine)
    │
    ├─ 1. Derives stable deployment UUID from SHA-256(NEO_SECRET_KEY)
    │      → same UUID every time for the same API key, no files needed
    │
    ├─ 2. Checks if VS Code/Cursor extension is running (localhost:31337)
    │      → if yes: skip daemon, extension handles execution
    │
    ├─ 3. No extension → auto-starts Python daemon in background
    │      → daemon polls /v2/poll/{uuid} using NEO_SECRET_KEY as Bearer token
    │
    ├─ 4. POST /v2/thread/init-chat-direct with deployment_id
    │      → backend routes task to the daemon
    │      → daemon executes: writes files, runs code, returns output
    │
    └─ 5. Background polling of /v2/thread/status/{thread_id} with API key
           → neo_task_status / neo_get_messages return results
```

### What "auto-start daemon" means

When `neo_submit_task` is called and no daemon is detected:
1. Server spawns `neo-mcp daemon` as a detached background process
2. Daemon looks for OAuth token in `~/.neo/daemon/mcp_auth.json` — **falls back to `NEO_SECRET_KEY`**
   if no OAuth token exists
3. Server waits up to 5s for daemon to register, then submits with the derived UUID

**The daemon falls back to the API key for polling.** This means `neo-mcp login` is optional
for the pip-installed stdio flow.

---

## Flow Comparison

| Setup | Login needed | Daemon needed | Task execution |
|---|---|---|---|
| VS Code/Cursor extension | No (extension handles it) | No (extension IS the daemon) | Local, on your machine |
| `pip install` + API key | **No** (key fallback) | Auto-started by MCP server | Local, on your machine |
| `pip install` + `neo-mcp login` | Yes (explicit OAuth) | Auto-started or manual | Local, full OAuth refresh |
| Hosted server (`mcpserver.heyneo.com`) | Yes (need daemon + deployment ID header) | Must run locally | Local via explicit daemon |

---

## The Two Auth Layers (unchanged — architectural context)

```
Editor / Claude Code
       │
       │  API key (sk-v1-...)
       ▼
MCP server (neo-mcp)            ← API key is sufficient for all MCP calls
  neo_submit_task   ──────────► POST /v2/thread/init-chat-direct   ✓ API key
  neo_task_status   ──────────► GET  /v2/thread/status/{thread_id} ✓ API key
  neo_get_messages  ──────────► GET  /v2/thread/thread-messages     ✓ API key
       │
       │  deployment_id (UUID derived from API key)
       ▼
Neo backend routes task to daemon
       │
       │  Bearer token (OAuth or API key fallback)
       ▼
Daemon on user's machine        ← execution layer, polls for commands
  polls ──────────────────────► GET /v2/poll/{deployment_id}
  executes locally, writes files
```

---

## Hosted Server Flow (mcpserver.heyneo.com)

The hosted server is stateless — it cannot auto-start a daemon on your machine. You must run
the daemon yourself and tell the server your deployment ID via header.

```bash
# Step 1: Authenticate (one-time)
neo-mcp login
# → opens browser → writes ~/.neo/daemon/mcp_auth.json

# Step 2: Start daemon (keep running)
neo-mcp daemon
# → polls /v2/poll/{uuid}, executes tasks locally
# → writes UUID to ~/.neo/daemon/standalone_deployment_id

# Step 3: Register MCP server with your deployment ID
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-your-key" \
  --header "X-Neo-Deployment-Id: $(cat ~/.neo/daemon/standalone_deployment_id)"
```

---

## VS Code / Cursor Extension Flow

No setup beyond installing the extension and logging in via the extension UI.

```bash
# MCP server (HTTP, hosted — recommended with extension)
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-your-key"
# Extension auto-detected → no deployment ID header needed

# MCP server (stdio, local install)
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-your-key \
  -- neo-mcp
# Extension detected on localhost:31337 → auto-used as daemon
```

Detection: MCP server checks for `~/.neo/daemon/daemon.token` + open socket on `127.0.0.1:31337`.

---

## What Was Implemented in v0.2.x

### v0.2.2 — Key-derived deployment ID + socket-based extension detection

**Before:** Daemon ID was read from `daemon.log` (only written by the Python daemon, not the
VS Code extension in production). Extension detection was unreliable; stale log entries blocked
auto-start after daemon died.

**After:**
- Deployment UUID derived from `SHA-256(NEO_SECRET_KEY)[:16]` — stable per user, no files needed
- VS Code extension detected by: `daemon.token` file exists + port 31337 open (live socket check)
- Auto-start flow: check `_register_with_daemon()` (VS Code extension) → check `_python_daemon_running()`
  → only auto-start if neither is active
- Fixed stale log bug: stale `daemon.log` entry no longer blocks daemon re-launch after crash

### v0.2.3 — Daemon auth fallback + setup wizard fix

- Daemon falls back to `NEO_SECRET_KEY` for `/v2/poll` auth when no OAuth token exists
- `neo-mcp setup` uses socket detection (not log parsing) for VS Code extension
- `run_setup()`: extension running → skip login + skip daemon start; no extension → login + daemon

### v0.2.4 — End-to-end test suite

- 29 e2e tests covering auth/connectivity, MCP tools, task submission, daemon detection,
  error handling, HTTP app routes — all passing against live Neo backend
- One test intentionally skipped: full poll-until-terminal cycle (requires daemon running)

---

## What Still Needs Backend Changes

### Change 1 (recommended) — Accept API key on `/v2/poll/{deployment_id}`

The Python daemon already sends `NEO_SECRET_KEY` as a fallback Bearer token. If the backend
stops rejecting non-OAuth tokens, OAuth login becomes completely optional.

```
# Current: 401 when API key sent to poll endpoint
# Proposed: 200 — deployment UUID is the capability credential (128-bit, derived from private key)

GET /v2/poll/{deployment_id}?max_messages=10&wait_time=5
Authorization: Bearer sk-v1-your-key   ← should be accepted
```

**Impact:** `neo-mcp login` is never needed. `neo-mcp daemon` works on first run with just the API key.

### Change 2 — Allow task submission without a running daemon

```
# Current: HTTP 400 when no healthy deployment registered
# Proposed: route to Neo cloud execution when no local daemon is available

POST /v2/thread/init-chat-direct
{ "message": "train a classifier", "deployment_type": "vscode" }
→ HTTP 200: { "thread_id": "..." }   ← cloud execution, no daemon needed
```

**Impact:** Zero-setup experience — add MCP server, set API key, tasks work immediately
without running any local process.

### Change 3 — Whitelist login relay redirect domain

For the remote server login relay (`mcpserver.heyneo.com/auth/callback`) to work, `heyneo.so/login`
must allow redirects to `mcpserver.heyneo.com`.

```
# Add to OAuth redirect allowlist on heyneo.so:
https://mcpserver.heyneo.com
```

**Impact:** `neo-mcp login` works on SSH/headless/remote servers where localhost callbacks fail.

---

## Priority Order

1. **Change 1** (accept API key on poll) — makes `neo-mcp login` entirely optional. The daemon
   already sends the key as fallback — just stop rejecting it.

2. **Change 2** (cloud execution fallback) — zero-setup for users who don't need local file access.

3. **Change 3** (login relay) — fixes broken login on remote servers. Only needed while Change 1
   is pending.

After Changes 1 + 2: any user can `pip install neo-mcp`, set `NEO_SECRET_KEY`, and submit tasks
with no additional steps — whether they want local or cloud execution.
