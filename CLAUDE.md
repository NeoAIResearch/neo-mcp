# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that wraps the Neo ML backend (`https://master.heyneo.so`). It exposes 9 tools to Claude Code so users can submit ML/AI tasks, poll status, read output, and control task lifecycle — via stdio or HTTP transport. The hosted server runs at `https://mcpserver.heyneo.com/mcp`.

## Project structure

Each concern lives in its own top-level folder.

```
neo-mcp/
├── python/                         # pip-installable MCP server (neo-mcp package)
│   ├── src/neo_mcp/
│   │   ├── server.py               # MCP server — all 9 tools, single file
│   │   ├── oauth.py                # OAuth 2.0 PKCE authorization server (HTTP mode)
│   │   ├── setup.py                # setup wizard (neo-mcp setup)
│   │   ├── daemon.py               # Python daemon for local task execution
│   │   └── login.py                # browser OAuth flow (neo-mcp login)
│   ├── tests/
│   │   ├── test_server.py          # 93-test unit suite (no key needed)
│   │   └── test_connection.py      # connectivity smoke test (needs key)
│   ├── scripts/start-daemon.sh     # standalone daemon launcher (bash)
│   ├── pyproject.toml              # package metadata + entry points
│   ├── requirements.txt            # runtime deps
│   ├── Dockerfile                  # HTTP-mode container (used by CI → ECR)
│   └── DEPLOYMENT.md               # Docker deployment guide
│
├── vscode_extension/               # VS Code/Cursor extension (TypeScript, own release cycle)
│
├── skills/                         # Agent framework integrations
│   ├── README.md                   # index
│   ├── claude-code/SKILL.md        # Claude Code /neo slash command
│   ├── vercel/SKILL.md             # Vercel AI SDK
│   ├── openai-agents/SKILL.md      # OpenAI Agents SDK
│   └── langchain/SKILL.md          # LangChain / LangGraph
│
├── docs/                           # Shared documentation
│   ├── CLIENTS.md                  # Editor setup guide
│   ├── USAGE.md                    # Usage guide + workflows
│   ├── CONNECTORS.md               # Claude.ai + ChatGPT web connector setup
│   └── WEB_CONNECTOR.md            # Web connector implementation notes
│
├── .github/workflows/
│   └── publish-mcp.yml             # CI: builds python/Dockerfile → ECR on push to main
├── README.md                       # Top-level overview + quick start
└── CLAUDE.md                       # This file
```

## Commands

```bash
# Install dependencies
cd python && pip install -r requirements.txt

# Set key (only NEO_SECRET_KEY required)
export NEO_SECRET_KEY=sk-v1-your-secret-key

# Run the server directly
python3 python/src/neo_mcp/server.py

# Or after pip install (from python/ directory):
cd python && pip install -e . && neo-mcp

# Start the Python daemon manually (normally auto-started by the agent on first task)
neo-mcp daemon

# Optional: browser OAuth login (only needed if daemon poll endpoint still requires OAuth)
neo-mcp login

# Run unit tests (no key needed)
python3 -m pytest python/tests/ -v

# Run connectivity test (requires NEO_SECRET_KEY)
NEO_SECRET_KEY=sk-v1-... python3 python/tests/test_connection.py

# Build Docker image
docker build -t neo-mcp-test ./python

# Run via Docker
docker run -i --rm -e NEO_SECRET_KEY=your-secret \
  -v ~/.neo:/root/.neo:ro neo-mcp-test

# Register with Claude Code (pip install — daemon auto-starts on first task)
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=your-secret \
  -- neo-mcp

# Register with Claude Code (hosted HTTP server — recommended, works for all editors)
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

**`python/src/neo_mcp/server.py`** is the entire server — no submodules, ~1000 lines.

### Auth
- Only `NEO_SECRET_KEY` is required (`sk-v1-...`) — passed as `Authorization: Bearer` on every request.
- `_headers()` raises `ValueError` with a clear message if `NEO_SECRET_KEY` is missing; the error is returned as a tool response (no crash).
- In HTTP mode, the per-request key from the `Authorization` header is stored in a context var (`_ctx_secret_key`) and takes priority over the module-level env var.

### Task submission — always vscode mode

`neo_submit_task` always submits with `deployment_type: "vscode"`. The submission path:

1. `_get_deployment_id()` — tries (in order): `X-Neo-Deployment-Id` header context var → `NEO_DEPLOYMENT_ID` env var → `_discover_sandbox_id()` → derives UUID from API key
2. In stdio mode, if daemon not running: `_auto_start_daemon()` launches the Python daemon automatically
3. In HTTP mode, if daemon not running: returns `DAEMON_NOT_RUNNING` message with `neo-mcp daemon &` command — the agent asks user permission and runs it
4. POSTs to `/v2/thread/init-chat-direct` with `deployment_type: "vscode"` and `deployment_id`
5. Returns `thread_id` immediately; background polling starts via `asyncio.create_task(_poll_task_bg(thread_id))`

**Important:** A `deployment_id` is **required** for Neo to route the task to an active daemon. The deployment UUID is derived deterministically from the API key (`SHA-256(key)[:16]` formatted as UUID) — same key always produces the same UUID, no files or headers needed.

### Deployment ID discovery
`_get_deployment_id()` checks (in priority order):
1. `_ctx_deployment_id` context var — set from `X-Neo-Deployment-Id` request header (HTTP mode override)
2. `NEO_DEPLOYMENT_ID` env var
3. `_discover_sandbox_id()`:
   a. Reads `~/.neo/daemon/daemon.log` — takes the last `{"sandboxId": "<uuid>"}` entry
   b. Reads `~/.neo/daemon/standalone_deployment_id` — UUID persisted by the Python daemon on first run
   c. Falls back to `~/.neo/daemon/thread-workspaces.json`
4. `_derive_deployment_id(secret_key)` — deterministic UUID from API key (always available when key is set)

### Hosted server vs local install — architecture split

The hosted server (`mcpserver.heyneo.com`) is a **stateless bridge** — it translates MCP calls to Neo API requests but never runs daemons.

**Execution always happens on the user's machine**, via:
- The Neo VS Code/Cursor extension (zero setup — handles everything automatically), OR
- The Python daemon — started automatically by the agent on first task submission

**Agent-driven daemon start (HTTP mode):**
When no daemon is found, `neo_submit_task` returns a `DAEMON_NOT_RUNNING` message with the exact command:
```
neo-mcp daemon &
```
Agents with terminal access (Claude Code, Cursor, Windsurf, Codex CLI) will ask user permission and run it. The user just clicks yes — no manual terminal work required.

**VS Code extension users** don't need any of this — the extension manages the daemon automatically.

### stdio mode (local pip install / Docker on user's machine)

In stdio mode the server and daemon run on the same machine. `neo_submit_task` auto-starts the daemon silently if it's not running, using a key-derived UUID. No user action needed.

### Thread-ID based polling — the core loop
After submission, `init-chat-direct` returns a `thread_id`. **All status and message queries use `thread_id`** — these APIs work with API key auth:

- `GET /v2/thread/status/{thread_id}` → status (RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / etc.)
- `GET /v2/thread/thread-messages?thread_id=...` → messages array

`_poll_task_bg(thread_id)` runs as a background asyncio task, polling status every 3–60 s (adaptive ramp), fetching all messages on COMPLETED. Results land in `_active_polls[thread_id]` so `neo_task_status` and `neo_get_messages` return instantly from cache.

### Daemon registration and heartbeat
When the VS Code/Cursor extension daemon is running:
- `_register_with_daemon(deployment_id, secret_key)` reads `~/.neo/daemon/daemon.token` and POSTs to `http://127.0.0.1:31337/register`
- `_heartbeat_loop(deployment_id)` sends a keepalive every 60 s to prevent the daemon from evicting the registration

When no daemon is running (stdio mode):
- `_auto_start_daemon(secret_key)` spawns the Python daemon (`neo-mcp daemon <cwd>`) as a detached subprocess, waits up to 5 s for its PID file

### Python daemon authentication (`src/neo_mcp/daemon.py`)

**Current state (pending backend change):** The daemon poll endpoint currently requires an OAuth token. The daemon reads `~/.neo/daemon/mcp_auth.json` written by `neo-mcp login` or the VS Code extension.

**Pending:** The backend team is adding API key support to the daemon poll endpoint (exact path TBD — likely `v2/thread/poll` or similar). Once shipped:
- Daemon authenticates with `NEO_SECRET_KEY` directly — no OAuth, no login step
- `neo-mcp login` becomes fully optional (only needed for OAuth-based token refresh)
- Every workflow becomes zero-touch: add the MCP server once, submit tasks, daemon auto-starts

Until that change ships, the daemon needs OAuth auth. `neo-mcp login` handles this and now **auto-starts the daemon** after successful login.

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

## Known constraints / pending backend changes

- **Daemon poll endpoint requires OAuth (pending fix):** The endpoint the daemon polls to receive execution commands currently only accepts OAuth tokens, not API keys. Once the backend adds API key support (path TBD — likely `v2/thread/poll` or similar), `neo-mcp login` becomes unnecessary and the entire setup collapses to a single `claude mcp add` command.
- Without the extension or Python daemon running, tasks submit and track correctly via thread_id polling — but local file execution depends on the daemon being active.
- Do NOT send a fabricated `NEO_DEPLOYMENT_ID` unless it matches a real running sandbox — it causes a 30 s ReadTimeout.

## Docker / CI

`Dockerfile` is in this directory (`neo-mcp/`). The GitHub Actions workflow at `.github/workflows/publish-mcp.yml` builds and pushes to the internal ECR registry on every push to `main`. The public Docker image (`ghcr.io/heyneo/neo-mcp-server`) is maintained separately.

PyPI releases trigger automatically on `v*` version tags.
