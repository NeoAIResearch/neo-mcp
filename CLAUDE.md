# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that wraps the Neo ML backend (`https://master.heyneo.so`). It exposes 9 tools to Claude Code so users can submit ML/AI tasks, poll status, read output, and control task lifecycle — via stdio or HTTP transport. The hosted server runs at `https://mcpserver.heyneo.com/mcp`.

## Project structure

```
neo-mcp/
├── src/neo_mcp/server.py   # MCP server — all tools, single file
├── src/neo_mcp/oauth.py    # OAuth 2.0 PKCE authorization server (HTTP mode)
├── src/neo_mcp/setup.py    # setup wizard (neo-mcp setup)
├── src/neo_mcp/daemon.py   # Python daemon for local task execution
├── docs/
│   ├── CLIENTS.md          # registration guide for all MCP clients
│   ├── USAGE.md            # user guide + deployment steps
│   ├── CONNECTORS.md       # web connector setup (Claude.ai + ChatGPT)
│   └── WEB_CONNECTOR.md    # web connector implementation notes
├── skills/neo/SKILL.md     # Claude Code skill definition (/neo command)
├── tests/
│   ├── test_connection.py
│   └── test_server.py
├── vscode_extension/       # VS Code/Cursor extension (TypeScript, separate release cycle)
├── .github/workflows/publish-mcp.yml
├── Dockerfile
├── pyproject.toml
└── requirements.txt
```

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Set key (only NEO_SECRET_KEY required)
export NEO_SECRET_KEY=sk-v1-your-secret-key

# Run the server directly
python3 src/neo_mcp/server.py

# Or after pip install:
neo-mcp

# Authenticate for local daemon (opens browser OAuth flow)
neo-mcp login

# Start the Python daemon (for local file execution, after login)
neo-mcp daemon

# Run unit tests (no key needed)
python3 -m pytest tests/ -v

# Run connectivity test (requires NEO_SECRET_KEY)
NEO_SECRET_KEY=sk-v1-... python3 tests/test_connection.py

# Build Docker image
docker build -t neo-mcp-test .

# Run via Docker
docker run -i --rm -e NEO_SECRET_KEY=your-secret \
  -v ~/.neo:/root/.neo:ro neo-mcp-test

# Register with Claude Code (pip install)
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=your-secret \
  -- neo-mcp

# Register with Claude Code (hosted HTTP server — no local install)
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer your-secret"

# Register with Claude Code (Docker, after publish)
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=your-secret \
  -- docker run -i --rm -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo:ro ghcr.io/heyneo/neo-mcp-server

# View MCP server logs
claude mcp logs neo
```

## Architecture

**`src/neo_mcp/server.py`** is the entire server — no submodules, ~1000 lines.

### Auth
- Only `NEO_SECRET_KEY` is required (`sk-v1-...`) — passed as `Authorization: Bearer` on every request.
- `_headers()` raises `ValueError` with a clear message if `NEO_SECRET_KEY` is missing; the error is returned as a tool response (no crash).
- In HTTP mode, the per-request key from the `Authorization` header is stored in a context var (`_ctx_secret_key`) and takes priority over the module-level env var.

### Task submission — always vscode mode

`neo_submit_task` always submits with `deployment_type: "vscode"`. There is no "cloud" mode. The submission path:

1. `_get_deployment_id()` — tries `NEO_DEPLOYMENT_ID` env var, then `_discover_sandbox_id()`
2. If no deployment_id found: `_auto_start_daemon()` launches the Python daemon in the background, waits up to 5 s for it to write its sandboxId to `daemon.log`, then calls `_get_deployment_id()` again
3. POSTs to `/v2/thread/init-chat-direct` with `deployment_type: "vscode"` and `deployment_id` if found (omitted if not)
4. Returns `thread_id` immediately; background polling starts via `asyncio.create_task(_poll_task_bg(thread_id))`

**Important:** the code never fabricates a deployment ID. If `_discover_sandbox_id()` finds nothing, the POST is sent without `deployment_id` — Neo handles this gracefully. Sending a fabricated UUID causes a 30-second `ReadTimeout` (Neo tries to reach a non-existent sandbox).

### Deployment ID discovery
`_get_deployment_id()` calls `_discover_sandbox_id()`:
1. Reads `~/.neo/daemon/daemon.log` line by line — takes the last `{"sandboxId": "<uuid>"}` entry
2. Falls back to `~/.neo/daemon/thread-workspaces.json` — prefers the workspace whose path matches CWD; falls back to the last entry
3. Returns `""` if neither file exists or contains a valid ID

The VS Code/Cursor extension writes these files when it starts up. The Python daemon writes to `daemon.log` when it registers itself.

### Thread-ID based polling — the core loop
After submission, `init-chat-direct` returns a `thread_id`. **All status and message queries use `thread_id`** — these APIs work with API key auth:

- `GET /v2/thread/status/{thread_id}` → status (RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / etc.)
- `GET /v2/thread/thread-messages?thread_id=...` → messages array

`_poll_task_bg(thread_id)` runs as a background asyncio task, polling status every 3–60 s (adaptive ramp), fetching all messages on COMPLETED. Results land in `_active_polls[thread_id]` so `neo_task_status` and `neo_get_messages` return instantly from cache.

### Daemon registration and heartbeat
When the VS Code/Cursor extension daemon is running:
- `_register_with_daemon(deployment_id, secret_key)` reads `~/.neo/daemon/daemon.token` and POSTs to `http://127.0.0.1:31337/register`
- `_heartbeat_loop(deployment_id)` sends a keepalive every 60 s to prevent the daemon from evicting the registration

When no daemon is running:
- `_auto_start_daemon(secret_key)` spawns the Python daemon (`neo-mcp daemon <cwd>`) as a detached subprocess, waits up to 3 s for its PID file

### Python daemon authentication (`src/neo_mcp/daemon.py`)
The Python daemon polls `/v2/poll/{deployment_id}` which requires an **OAuth token**, not the `NEO_SECRET_KEY` API key.

**Auth file:** `~/.neo/daemon/mcp_auth.json` — `{"access_token": "...", "refresh_token": "...", "username": "..."}`

This file is written by:
- **`neo-mcp login`** (`src/neo_mcp/login.py`) — opens `https://heyneo.so/login?redirect=http://localhost:{port}/callback` in the browser, waits for the OAuth callback, then writes the token. Fallback: manual token paste prompt.
- **VS Code/Cursor extension** — writes the file automatically when logged in via the extension UI.

The daemon exits with a clear error if no valid token is found: `"Run 'neo-mcp login' to authenticate"`.
On 401 from the poll endpoint, the daemon calls `POST /auth/refresh-token` with `{username, refreshToken}` and retries once.

### Other design points
- `NEO_READ_ONLY=true` strips all write tools at `list_tools()` time — only `neo_task_status`, `neo_task_plan`, `neo_get_messages`, and `neo_get_files` remain.
- `NEO_WORKSPACE_DIR` overrides `os.getcwd()` for `_server_cwd` — useful in Docker.
- `handle_error(status_code)` is the single error-mapping function; every tool calls it on non-200 responses.
- `neo_get_messages` paginates via `before=<timestamp>` cursor and hard-caps at 80 000 chars (~20 000 tokens).
- Thread ID is persisted to `~/.neo/active_thread_id` so follow-up tool calls can recover it without the caller re-supplying it.
- Transport: `stdio_server` from `mcp.server.stdio` for stdio mode; Starlette + uvicorn for HTTP mode (`NEO_TRANSPORT=http`).

## Tool → route mapping

| Tool | Method | Path |
|---|---|---|
| `neo_submit_task` | POST | `/v2/thread/init-chat-direct` |
| `neo_task_status` | GET | `/v2/thread/status/{thread_id}` |
| `neo_get_messages` | GET | `/v2/thread/thread-messages` |
| `neo_send_feedback` | POST | `/v2/thread/feedback/{thread_id}` |
| `neo_pause_task` | POST | `/v2/thread/control/{thread_id}` (signal: PAUSE) |
| `neo_resume_task` | POST | `/v2/thread/control/{thread_id}` (signal: RESUME) |
| `neo_stop_task` | DELETE | `/v2/thread/cleanup-direct/{thread_id}` |

Auth on every request: `Authorization: Bearer $NEO_SECRET_KEY`

## What does NOT work / known constraints
- `/v2/poll/{deployment_id}` — requires OAuth token (browser login), not API keys. The VS Code/Cursor extension handles that side of polling using its own OAuth token. The Python daemon also polls this endpoint and requires the key to be exchanged for an OAuth token via the Neo auth flow.
- Without the extension or Python daemon running, tasks still submit and track correctly via thread_id polling — but local file execution (writing files to your machine) depends on the daemon being active.
- Do NOT send a fabricated or pinned `NEO_DEPLOYMENT_ID` unless it matches a real running sandbox — it causes a 30 s ReadTimeout.

## Docker / CI

`Dockerfile` is in this directory (`neo-mcp/`). The GitHub Actions workflow at `.github/workflows/publish-mcp.yml` builds and pushes to the internal ECR registry on every push to `main`. The public Docker image (`ghcr.io/heyneo/neo-mcp-server`) is maintained separately.

PyPI releases trigger automatically on `v*` version tags.
