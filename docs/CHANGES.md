# Neo MCP Authentication

## The Core Problem

When a user adds the Neo MCP server and tries to submit a task, they get:

```
HTTP 400 — No healthy deployments available
```
ee
This happens because the Neo backend requires an active **daemon** process polling
`/v2/poll/{deployment_id}` before it will accept any task submission. The daemon
is the execution layer — it receives task commands and runs them on the user's machine.

The daemon's poll endpoint requires an **OAuth token**, not the API key (`sk-v1-...`).
So every new user has to:

1. Run `neo-mcp login` → browser OAuth flow → get token
2. Run `neo-mcp daemon` → daemon starts polling with that token
3. Only then does task submission work

This is too much friction, especially on remote servers where browser OAuth is broken.

---

## The Two Auth Layers (and why they're separate)

```
Editor / Claude Code
       │
       │  API key (sk-v1-...)
       ▼
mcpserver.heyneo.com        ← stateless bridge, API key only
  neo_submit_task   ──────► POST /v2/thread/init-chat-direct   ✓ API key works
  neo_task_status   ──────► GET  /v2/thread/status/{thread_id} ✓ API key works
  neo_get_messages  ──────► GET  /v2/thread/thread-messages     ✓ API key works
       │
       │  requires deployment_id pointing to a running daemon
       ▼
Neo backend
       │
       │  OAuth token (browser login)
       ▼
Daemon on user's machine    ← local execution layer, OAuth only
  polls ──────────────────► GET /v2/poll/{deployment_id}        ✗ API key rejected
  executes tasks locally
  writes files to filesystem
```

**The MCP server already works fine with just the API key for everything except task submission,
which fails if no daemon is running.**

---

## Short-Term Fix: MCP-Side Polling (no backend changes needed)

The MCP server already does thread-based status polling via API key. The flow works today for
users who have a daemon running:

1. `neo_submit_task` → POSTs to `/v2/thread/init-chat-direct` → returns `thread_id`
2. Background task `_poll_task_bg(thread_id)` starts immediately, polling
   `GET /v2/thread/status/{thread_id}` every 3–60s with the API key
3. User calls `neo_task_status` → reads from the in-memory cache, returns live status
4. When COMPLETED → messages fetched via `GET /v2/thread/thread-messages`

This is already implemented in `server.py`. The MCP **does not need the daemon for status
tracking** — only for task execution (running code, writing files on the user's machine).

So the current state:
- Status polling: ✓ works with API key, no daemon needed
- Task execution: ✗ needs daemon (which needs OAuth)

---

## How the VS Code Extension Daemon Works (the reference implementation)

The VS Code extension daemon is the reference for how polling is supposed to work:

```
1. User logs in via extension UI → gets OAuth session token
2. Extension generates deployment ID (UUIDv4, stored in VS Code globalState per user+machine)
3. Spawns PollerDaemon.js (Node.js process listening on localhost:31337)
4. Extension registers with daemon:
     POST localhost:31337/register
     { deploymentId, workspaceFolder, authToken: <OAuth token> }
5. Daemon starts polling backend:
     GET /v2/poll/{deploymentId}?max_messages=10&wait_time=5
     Authorization: Bearer <OAuth token>    ← this is the only auth the backend accepts here
6. Backend pushes commands → daemon executes locally:
     write_code    → writes files to workspaceFolder
     run_subprocess → runs shell commands
     get_file      → reads files
     list_files    → lists directories
7. Daemon POSTs results back:
     POST /v2/poll/{deploymentId}/response
     Authorization: Bearer <OAuth token>
```

## Our Python Daemon is Identical — Except Auth

The Python daemon (`neo-mcp daemon`) replicates this flow exactly:

```
1. neo-mcp login → browser OAuth → writes token to ~/.neo/daemon/mcp_auth.json
2. Derives deployment UUID from SHA-256(NEO_SECRET_KEY) — stable per user, no globalState needed
3. No local HTTP server needed (no VS Code IPC layer)
4. Polls backend directly:
     GET /v2/poll/{deploymentId}
     Authorization: Bearer <token from mcp_auth.json>
     Falls back to: Bearer <NEO_SECRET_KEY>  ← backend rejects this with 401
5. Executes commands, writes files, runs subprocesses
6. POSTs results back
```

**They are structurally identical.** The Python daemon is a direct Python port of the VS Code
extension daemon. The only difference is auth on step 4:

| | Auth token used on `/v2/poll` | Works? |
|---|---|---|
| VS Code extension daemon | OAuth session token (from extension login) | ✓ |
| Python daemon (with `neo-mcp login`) | OAuth token from `mcp_auth.json` | ✓ |
| Python daemon (without login) | `NEO_SECRET_KEY` fallback | ✗ 401 |

**The entire login requirement — `neo-mcp login`, browser OAuth, `mcp_auth.json` —
exists for one single reason: `/v2/poll/{deploymentId}` rejects `sk-v1-...` API keys.**

---

## Why the VS Code Extension Requires Auth for Deployment ID — and Why It Doesn't Have To

From `StateManager.ts` line 225:
```typescript
throw new Error('Cannot get deployment ID: No user logged in. Deployment ID requires authentication.');
```

The extension gates deployment ID creation behind login. But the reason is **identity**, not security — it derives the UUID from `MD5(userId + machineId + remoteName)` to ensure each user gets a unique, stable UUID per machine. Login is just used to get `userId`.

**Our Python daemon achieves the same thing differently:** UUID = `SHA-256(NEO_SECRET_KEY)`. The API key IS the user identity. No login needed to derive a stable, per-user UUID.

**This means auth on the poll endpoint is redundant** — if the UUID is:
- 128 bits (SHA-256 truncated) — impossible to brute force
- Derived from the private API key — only the key holder can compute it
- Not shared publicly — only lives in the daemon process

...then knowing the UUID is equivalent to being authenticated. The Bearer token on `/v2/poll` adds no security that the UUID doesn't already provide.

---

## The Real Fix: Two Backend Changes

### Change 1 (highest impact) — Make the poll endpoint auth-optional

**Proposal:** `/v2/poll/{deployment_id}` should work with just the UUID in the URL — no Bearer token required. The UUID IS the credential.

**Why this is safe:**
- UUID is 128-bit, derived from private API key — not guessable
- Possession of the UUID = authorization to poll that deployment
- This is standard capability-based access control (same model as Zoom links, Google Docs share links, webhook URLs)
- The VS Code extension uses auth only because it needs to look up `userId` — not because the poll endpoint demands it

**Why this is correct:**
- The API key already authenticates task submission and status checks
- The daemon UUID is derived from the same API key — it IS the user
- Adding a separate OAuth token creates a second credential that can go stale, expire, and require refresh — all complexity that serves no real security purpose

**Backend change:**
```
GET /v2/poll/{deployment_id}?max_messages=10&wait_time=5
# No Authorization header required
# UUID in URL is the credential — 128-bit, unguessable

POST /v2/poll/response
# Similarly, no auth required — or accept any non-empty Bearer token
```

**Client side (already done):** Python daemon already sends `NEO_SECRET_KEY` as fallback Bearer token. If the backend stops rejecting non-OAuth tokens, the daemon works immediately with zero changes.

**Impact:**
- `neo-mcp login` is no longer needed — ever
- `neo-mcp daemon` works on first run: `NEO_SECRET_KEY=sk-v1-... neo-mcp daemon`
- No `mcp_auth.json`, no browser, no OAuth
- `neo-mcp setup` = enter API key → configure editor → done in 30 seconds

---

### Change 2 — Allow task submission without a deployment ID

**Proposal:** When `POST /v2/thread/init-chat-direct` is called with no active daemon registered, run the task on Neo's cloud execution layer instead of returning 400.

**Current:**
```
POST /v2/thread/init-chat-direct
{ "message": "train a classifier", "deployment_type": "vscode" }

→ HTTP 400: No healthy deployments available
```

**Proposed:**
```
POST /v2/thread/init-chat-direct
{ "message": "train a classifier", "deployment_type": "vscode" }

→ HTTP 200: { "thread_id": "..." }  ← runs on cloud, no daemon needed
```

**Why:** Not every task needs local file access. ML training, data processing, code generation — these run entirely in Neo's cloud. The 400 forces all users to set up a daemon even for pure cloud tasks.

**Impact:**
- Zero setup for cloud tasks — add MCP server, set API key, submit tasks immediately
- Daemon becomes opt-in: only needed when tasks must write to the local filesystem
- Eliminates the 400 error that currently blocks every new user on first run

---

## What the Flow Looks Like After Backend Changes

### Minimal flow (no daemon, cloud execution)

```bash
# 1. Add MCP server to editor with API key — one time
claude mcp add --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-your-key"

# 2. Submit tasks immediately — no login, no daemon
# neo_submit_task → runs on Neo cloud → MCP polls thread_id → done
```

### Local execution flow (daemon, no OAuth)

```bash
# After Change 1 (API key accepted on poll endpoint):
neo-mcp daemon   # starts polling with NEO_SECRET_KEY, no login needed
neo_submit_task  # routes to local daemon, executes on user's machine
```

### Full local flow today (before backend changes)

```bash
neo-mcp login    # browser OAuth → mcp_auth.json
neo-mcp daemon   # polls with OAuth token
neo_submit_task  # routes to local daemon
```

---

## The Login Relay (for remote servers, until Change 1 ships)

Until the backend accepts the API key on the poll endpoint, users on remote servers
(SSH, cloud VMs, Codespaces) hit a broken login experience: the localhost callback
never fires because their browser is on a different machine.

The fix implemented in `login.py` + `server.py` is a relay through `mcpserver.heyneo.com`:

```
neo-mcp login
  │
  ├─ POST https://mcpserver.heyneo.com/auth/pending/{state}   (register)
  │
  ├─ Print: "Open https://heyneo.so/login?redirect=
  │          https://mcpserver.heyneo.com/auth/callback?state={uuid}"
  │
  ├─ Poll https://mcpserver.heyneo.com/auth/poll/{state} every 2s
  │
  │         User opens URL, logs in on heyneo.so
  │         Neo redirects to mcpserver.heyneo.com/auth/callback
  │         Token stored in relay server memory (5 min TTL)
  │
  └─ Poll returns token → write ~/.neo/daemon/mcp_auth.json → done
```

### What needs to ship for the relay to work

| Task | Owner | Status |
|---|---|---|
| Deploy new `server.py` to `mcpserver.heyneo.com` (adds `/auth/callback`, `/auth/pending`, `/auth/poll` endpoints) | Backend / CI | Not deployed |
| Whitelist `mcpserver.heyneo.com` as a valid redirect domain on `heyneo.so/login` | Auth / Frontend | Not done |
| URL-encode the redirect param if heyneo.so requires it | Backend | Test after deploy |

Once shipped, `neo-mcp login` on any machine — local, SSH, headless — will print one URL,
wait silently, and complete automatically when the user logs in from any browser.

---

## Priority Order

1. **Backend Change 2** (allow submission without deployment_id) — unblocks all users
   immediately with zero setup. Highest impact.

2. **Backend Change 1** (accept API key on poll endpoint) — eliminates OAuth requirement
   for local daemon. Makes `neo-mcp setup` a one-step flow.

3. **Login relay deploy** (whitelist + deploy server.py) — fixes the broken login UX for
   remote server users. Only needed while Change 1 is pending.

Once 1 and 2 are done, OAuth login becomes entirely optional and the relay can be
kept as a convenience rather than a requirement.

---

## Exact Backend Changes Required

### Change 1 — `/v2/poll/{deployment_id}` — stop requiring OAuth

**File/service:** Backend polling router
**Change type:** Auth middleware / guard

```python
# Before: only accepts OAuth session tokens
if not is_valid_oauth_token(bearer_token):
    return 401

# After: accept OAuth token OR api key OR no token at all
# The deployment UUID in the URL is the capability credential
# (128-bit SHA-256 derived from user's API key — not guessable)
# if bearer_token provided, optionally validate it — but don't block on missing/non-OAuth
```

Endpoints to update:
- `GET  /v2/poll/{deployment_id}` — polling for commands
- `POST /v2/poll/response` — sending execution results back

**Test:**
```bash
# Should return 200 (or 202/empty) not 401
curl "https://master.heyneo.so/v2/poll/{uuid}?max_messages=1&wait_time=0"
curl "https://master.heyneo.so/v2/poll/{uuid}?max_messages=1&wait_time=0" \
  -H "Authorization: Bearer sk-v1-any-api-key"
```

---

### Change 2 — `/v2/thread/init-chat-direct` — don't 400 on missing deployment

**File/service:** Thread initialization router
**Change type:** Deployment routing logic

```python
# Before
if not get_healthy_deployment(deployment_id):
    return 400, "No healthy deployments available"

# After
if not get_healthy_deployment(deployment_id):
    route_to_cloud_execution()  # run on Neo cloud instead of local daemon
    # return thread_id as normal
```

**Test:**
```bash
# Should return 200 + thread_id, not 400
curl -X POST "https://master.heyneo.so/v2/thread/init-chat-direct" \
  -H "Authorization: Bearer sk-v1-your-key" \
  -H "Content-Type: application/json" \
  -d '{"message": "hello", "deployment_type": "vscode"}'
```

---

### Change 3 (login relay) — whitelist redirect domain on `heyneo.so/login`

**File/service:** heyneo.so frontend auth config / OAuth redirect allowlist
**Change type:** Config / allowlist

```
# Add to allowed redirect origins:
https://mcpserver.heyneo.com
```

Without this, `heyneo.so/login?redirect=https://mcpserver.heyneo.com/auth/callback?state=...`
will be blocked or ignored by the login page.

**Test:**
Open in browser — should redirect to mcpserver after login, not throw an error:
```
https://heyneo.so/login?redirect=https://mcpserver.heyneo.com/auth/callback?state=test-123
```
