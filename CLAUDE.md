# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that wraps the Neo ML backend (`https://master.heyneo.so`). It exposes 10 tools to Claude Code so users can submit ML/AI tasks, poll status, read output, and control task lifecycle — via stdio or HTTP transport. The hosted server runs at `https://mcpserver.heyneo.com/mcp`.

## Project structure

Each concern lives in its own top-level folder.

```
neo-mcp/
├── python/                         # pip-installable MCP server (neo-mcp package)
│   ├── src/neo_mcp/
│   │   ├── server.py               # MCP server — all 10 tools, single file
│   │   ├── oauth.py                # OAuth 2.0 PKCE authorization server (HTTP mode)
│   │   ├── setup.py                # setup wizard (neo-mcp setup)
│   │   ├── daemon.py               # Python daemon (fallback — primary is npm daemon)
│   │   └── login.py                # browser OAuth flow (neo-mcp login)
│   ├── tests/
│   │   ├── test_server.py          # 129-test unit suite (no key needed)
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
├── NPM_DAEMON_PLAN.md              # Plan for npm daemon package (neo-mcp-daemon)
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

# Start the npm daemon (PRIMARY — required for task execution)
npx neo-mcp-daemon /path/to/workspace &

# Start the Python daemon manually (fallback if npx not available)
neo-mcp daemon

# Run unit tests (no key needed)
python3 -m pytest python/tests/ -v

# Run connectivity test (requires NEO_SECRET_KEY)
NEO_SECRET_KEY=sk-v1-... python3 python/tests/test_connection.py

# Build Docker image
docker build -t neo-mcp-test ./python

# Run via Docker
docker run -i --rm -e NEO_SECRET_KEY=your-secret \
  -v ~/.neo:/root/.neo:ro neo-mcp-test

# Register with Claude Code (PRIMARY — hosted HTTP server, works for all editors)
# On first task, agent asks permission to start the local daemon (one click).
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer your-secret"

# Register with Claude Code (local pip install — daemon auto-starts silently in stdio mode)
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=your-secret \
  -- neo-mcp

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

### Daemon — Go binary is primary

**The primary daemon is the Go binary at `~/.neo/agent`**, installed automatically by `npx neo-mcp-daemon` (via `postinstall.js` + bin entrypoint). The Node.js npm daemon and Python daemon (`daemon.py`) are fallbacks for platforms without a pre-built Go binary.

The daemon executes tasks on the user's machine — polls the Neo backend for commands (`write_code`, `run_subprocess`, `get_file`, `list_files`, etc.) and runs them locally.

`_ensure_local_daemon()` startup priority:
1. Go binary already running → done
2. `~/.neo/agent` exists → `_auto_start_go_daemon()` (preferred)
3. `npx neo-mcp-daemon` available → `_auto_start_npm_daemon()` (which itself installs + execs Go binary)
4. Python daemon → last resort

Deployment UUID: derived from `NEO_SECRET_KEY` via SHA-256 — both the hosted server and all daemon types use the same formula, so no coordination needed.

- **Go daemon** writes PID to `~/.neo/daemon/go_daemon.pid`
- **npm daemon** writes PID to `~/.neo/daemon/npm_daemon.pid`
- **Python daemon** writes PID to `~/.neo/daemon/python_daemon.pid`
- All write `{"sandboxId": "..."}` entries to `~/.neo/daemon/daemon.log`
- All write thread→workspace mappings to `~/.neo/daemon/thread-workspaces.json`

### Task submission — always vscode mode

`neo_submit_task` always submits with `deployment_type: "vscode"`. The submission path:

1. `_get_deployment_id()` — tries (in order): `X-Neo-Deployment-Id` header context var → `NEO_DEPLOYMENT_ID` env var → `_discover_sandbox_id()` → derives UUID from API key
2. In stdio mode, if no daemon running: `_auto_start_npm_daemon()` launches `npx neo-mcp-daemon` as a detached subprocess, waits up to 5 s for its PID file
3. In HTTP mode, if no daemon running: returns `DAEMON_NOT_RUNNING` message with `npx neo-mcp-daemon &` — the agent asks user permission and runs it
4. POSTs to `/v2/thread/init-chat-direct` with `deployment_type: "vscode"` and `deployment_id`
5. Returns `thread_id` immediately; background polling starts via `asyncio.create_task(_poll_task_bg(thread_id))`

**Important:** A `deployment_id` is **required** for Neo to route the task to an active daemon. The deployment UUID is derived deterministically from the API key (`SHA-256(key)[:16]` formatted as UUID) — same key always produces the same UUID, no files or headers needed.

### Deployment ID discovery
`_get_deployment_id()` checks (in priority order):
1. `_ctx_deployment_id` context var — set from `X-Neo-Deployment-Id` request header (HTTP mode override)
2. `NEO_DEPLOYMENT_ID` env var
3. `_discover_sandbox_id()`:
   a. Reads `~/.neo/daemon/daemon.log` — takes the last `{"sandboxId": "<uuid>"}` entry
   b. Reads `~/.neo/daemon/standalone_deployment_id` — UUID persisted by the daemon on first run
   c. Falls back to `~/.neo/daemon/thread-workspaces.json`
4. `_derive_deployment_id(secret_key)` — deterministic UUID from API key (always available when key is set)

### Hosted server vs local install — architecture split

The hosted server (`mcpserver.heyneo.com`) is a **stateless bridge** — it translates MCP calls to Neo API requests but never runs daemons.

**Execution always happens on the user's machine**, via:
- The Neo VS Code/Cursor extension (zero setup — handles everything automatically), OR
- The npm daemon (`npx neo-mcp-daemon`) — started automatically by the agent on first task submission

**Agent-driven daemon start (HTTP mode):**
When no daemon is found, `neo_submit_task` returns a `DAEMON_NOT_RUNNING` message with the exact startup command — `NEO_SECRET_KEY` pre-filled:
```
NEO_SECRET_KEY=sk-v1-... ~/.neo/agent --daemon >/tmp/neo-daemon.log 2>&1 &
```
Agents with terminal access (Claude Code, Cursor, Windsurf, Codex CLI) ask user permission and run it. The user clicks **Allow** once — the Go binary installs via `npx` if not present, starts, and tasks flow automatically. No manual terminal work required.

**VS Code extension users** don't need any of this — the extension manages the daemon automatically.

### stdio mode (local pip install / Docker on user's machine)

In stdio mode the server and daemon run on the same machine. `neo_submit_task` auto-starts `npx neo-mcp-daemon` silently if it's not running. No user action needed.

### Thread-ID based polling — the core loop
After submission, `init-chat-direct` returns a `thread_id`. **All status and message queries use `thread_id`** — these APIs work with API key auth:

- `GET /v2/thread/status/{thread_id}` → status (RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / etc.)
- `GET /v2/thread/thread-messages?thread_id=...` → messages array

`_poll_task_bg(thread_id)` runs as a background asyncio task, polling status every 3–60 s (adaptive ramp), fetching all messages on COMPLETED. Results land in `_active_polls[thread_id]` so `neo_task_status` and `neo_get_messages` return instantly from cache.

### neo_get_files — local workspace, no S3

`neo_get_files` reads files **directly from the local workspace** that the daemon wrote to. There is no S3, no export job, no presigned URLs.

Flow:
1. Look up the workspace path for the thread from `~/.neo/daemon/thread-workspaces.json` (`_THREAD_WORKSPACES_FILE`)
2. Fall back to `_server_cwd` if no mapping found
3. Walk the workspace directory (skipping `venv`, `node_modules`, `.git`, `__pycache__`, etc.)
4. Return file contents inline, capped at 80 000 chars

This works because the daemon writes files locally via `write_code` commands, and `thread-workspaces.json` records which workspace was used for each thread.

**Do not add S3 / export-artifacts / presigned URL logic back** — it was a leftover from a cloud execution model that no longer applies.

### Daemon registration and heartbeat
When the VS Code/Cursor extension daemon is running:
- `_register_with_daemon(deployment_id, secret_key)` reads `~/.neo/daemon/daemon.token` and POSTs to `http://127.0.0.1:31337/register`
- `_heartbeat_loop(deployment_id)` sends a keepalive every 60 s to prevent the daemon from evicting the registration

### Other design points
- `NEO_READ_ONLY=true` strips all write tools at `list_tools()` time — only `neo_task_status`, `neo_task_plan`, `neo_get_messages`, and `neo_get_files` remain.
- `NEO_WORKSPACE_DIR` overrides `os.getcwd()` for `_server_cwd` — useful in Docker.
- `handle_error(status_code)` is the single error-mapping function; every tool calls it on non-200 responses.
- `neo_get_messages` paginates via `before=<timestamp>` cursor and hard-caps at 80 000 chars (~20 000 tokens).
- Thread ID is persisted to `~/.neo/active_thread_id` (`_THREAD_ID_FILE`) so follow-up tool calls can recover it without the caller re-supplying it.
- Thread→workspace mapping is persisted to `~/.neo/daemon/thread-workspaces.json` (`_THREAD_WORKSPACES_FILE`) — written by the daemon, read by `neo_get_files`.
- Transport: `stdio_server` from `mcp.server.stdio` for stdio mode; Starlette + uvicorn for HTTP mode (`NEO_TRANSPORT=http`).

## Tool → route mapping

| Tool | Method | Path |
|---|---|---|
| `neo_submit_task` | POST | `/v2/thread/init-chat-direct` |
| `neo_list_tasks` | GET (optional) | `/v2/thread/list` + in-memory + `~/.neo/active_thread_id` |
| `neo_task_status` | GET | `/v2/thread/status/{thread_id}` |
| `neo_task_plan` | GET | `/v2/thread/status/{thread_id}` (reads `current_plan` field) |
| `neo_get_messages` | GET | `/v2/thread/thread-messages` |
| `neo_get_files` | local | reads from `~/.neo/daemon/thread-workspaces.json` → local workspace |
| `neo_send_feedback` | POST | `/v2/thread/feedback/{thread_id}` |
| `neo_pause_task` | POST | `/v2/thread/control/{thread_id}` (signal: PAUSE) |
| `neo_resume_task` | POST | `/v2/thread/control/{thread_id}` (signal: RESUME) |
| `neo_stop_task` | DELETE | `/v2/thread/cleanup-direct/{thread_id}` |

Auth on every request: `Authorization: Bearer $NEO_SECRET_KEY`

## Known constraints

- Without the npm daemon running, tasks submit and track correctly via thread_id polling — but local file execution depends on the daemon being active.
- Do NOT send a fabricated `NEO_DEPLOYMENT_ID` unless it matches a real running sandbox — it causes a 30 s ReadTimeout.
- `neo_get_files` reads from the local workspace. If called from the hosted MCP server (`mcpserver.heyneo.com`) without local filesystem access, it will fall back to `_server_cwd` which may not be the user's workspace.

## Docker / CI

`Dockerfile` is in `python/`. The GitHub Actions workflow at `.github/workflows/publish-mcp.yml` builds and pushes to the internal ECR registry on every push to `main`. The public Docker image (`ghcr.io/heyneo/neo-mcp-server`) is maintained separately.

PyPI releases trigger automatically on `v*` version tags.
