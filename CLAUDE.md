# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that wraps the Neo ML backend (`https://master.heyneo.so`). It exposes 7 tools to Claude Code so users can submit ML/AI tasks, poll status, read output, and control task lifecycle — via stdio or HTTP transport. The hosted server runs at `https://mcpserver.heyneo.com/mcp`.

Current pip version: **0.4.16**.

## Project structure

Each concern lives in its own top-level folder.

```
neo-mcp/
├── python/                         # pip-installable MCP server (neo-mcp package)
│   ├── src/neo_mcp/
│   │   ├── server.py               # MCP server — all 7 tools, single file
│   │   ├── oauth.py                # OAuth 2.0 PKCE authorization server (HTTP mode)
│   │   ├── setup.py                # setup wizard (neo-mcp setup)
│   │   ├── daemon.py               # Python daemon (fallback — primary is npm daemon)
│   │   └── login.py                # browser OAuth flow (neo-mcp login)
│   ├── tests/
│   │   ├── test_concurrent_workspaces.py  # primary unit suite (60 tests, no key needed)
│   │   ├── test_server.py                 # legacy tests (stale — references old API)
│   │   └── test_connection.py             # connectivity smoke test (needs key)
│   ├── scripts/start-daemon.sh     # standalone daemon launcher (bash)
│   ├── pyproject.toml              # package metadata + entry points
│   ├── requirements.txt            # runtime deps
│   ├── Dockerfile                  # HTTP-mode container (used by CI → ECR)
│   └── DEPLOYMENT.md               # Docker deployment guide
│
├── npm/                            # npm daemon package (neo-mcp-daemon)
│   ├── src/
│   │   ├── daemon.ts               # polling loop + command dispatch
│   │   ├── executor.ts             # write_code, run_subprocess, etc. + path remapping
│   │   └── mcp-server.ts           # MCP server with all 7 tool definitions
│   ├── bin/neo-mcp-daemon          # CLI entry point
│   ├── package.json                # npm package metadata (neo-mcp-daemon)
│   └── tsconfig.json
│
├── vscode_extension/               # VS Code/Cursor extension (TypeScript, own release cycle)
│
├── skills/                         # Agent framework integrations
│   ├── README.md                   # index
│   ├── claude-code/SKILL.md        # Claude Code /neo slash command
│   ├── neo-setup/SKILL.md          # /neo-setup onboarding guide
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

The Python server is split into focused modules (not a single monolithic file):

| Module | Role |
|---|---|
| `server.py` | MCP server wiring — 7 tool definitions + call dispatch |
| `backend_client.py` | Async HTTP client for all Neo API routes |
| `backend_poller.py` | Background poll loop — fetches commands, dispatches them, sends responses |
| `action_handlers.py` | Executes commands locally — write_code, get_file, run_subprocess, list_files, create_session |
| `job_manager.py` | Async subprocess lifecycle — create, stream output, terminate, cleanup |
| `auth.py` | Key resolution + deterministic deployment UUID derivation |
| `paths.py` | All `~/.neo/daemon/` path constants in one place |
| `config.py` | Tunable constants — poll intervals, timeouts, API URL |

### Auth
- Only `NEO_SECRET_KEY` is required (`sk-v1-...`) — passed as `Authorization: Bearer` on every request.
- `BackendClient._headers()` raises `ValueError` with a clear message if `NEO_SECRET_KEY` is missing; the error surfaces as a tool response (no crash).
- In HTTP mode, the per-request key from the `Authorization` header takes priority over the env var.

### Daemon — npm daemon is primary

**The primary daemon is the npm daemon (`npx neo-mcp-daemon`)**. The Python BackendPoller (built into the pip package) is the fallback when no other daemon is running.

The daemon executes tasks on the user's machine — polls the Neo backend for commands (`write_code`, `run_subprocess`, `get_file`, `list_files`, etc.) and runs them locally.

Startup priority (in `server.py`):
1. `NEO_NO_DAEMON=true` is set → skip all daemon startup (bridge/hosted mode)
2. npm daemon already running (`npm_daemon.pid` alive) → skip Python poller
3. Python poller starts as a background asyncio task alongside the MCP server

Deployment UUID: derived from `NEO_SECRET_KEY` via SHA-256 — both the hosted server and all daemon types use the same formula, so no coordination needed.

- **npm daemon** writes PID to `~/.neo/daemon/npm_daemon.pid`
- **Python poller** writes lock to `~/.neo/daemon/neo-mcp.lock`
- All write `{"sandboxId": "..."}` entries to `~/.neo/daemon/daemon.log`
- All write thread→workspace mappings to `~/.neo/daemon/thread-workspaces.json`

### Path remapping — how local files land in the right place

The Neo backend runs tasks inside a container where paths are prefixed with `/app/project/`. The daemon remaps these to the user's local workspace.

`remapToWorkspace` / `_remap_to_workspace` strips known container prefixes (`/app/project`, `/app`, `/workspace`, `/project`) and resolves the relative remainder against the local workspace path.

**Deduplication:** if the workspace directory name matches the first segment of the remapped relative path, that segment is stripped. Example: workspace `/home/user/test_2`, Neo sends `/app/project/test_2/model.py` → remaps to `/home/user/test_2/model.py` (not `/home/user/test_2/test_2/model.py`).

**Exact-root handling:** `/app/project` with no trailing slash (rare) is handled as an exact match → maps to workspace root.

**Adaptive polling:** the daemon uses `wait_time=1` for long-poll requests when a command was received within the last 60 s (active execution), and `wait_time=5` when idle. This reduces worst-case per-file latency from ~5 s to ~1 s during active task execution.

**Workspace must be the git/project ROOT** — never a subdirectory. Passing a subdirectory causes files to land in a duplicate nested folder.

### Path security — file operations are sandboxed

All file reads and writes are validated to be within the thread's workspace or `/tmp`:
- `write_code` with a relative filename: `_is_allowed_path` blocks traversal (e.g. `../../etc/passwd`). Returns `"Path traversal detected"`.
- `write_code` with an absolute container path (e.g. `/app/project/src/main.py`): remapped to workspace via `_remap_to_workspace`, never written to the container path directly.
- `get_file` with a relative path: same traversal check applied after resolving.
- `get_file` with an absolute path outside workspace: remapped (not read directly). A compromised backend cannot read `/etc/passwd` or `~/.ssh/id_rsa`.

### Job lifecycle and memory management

`JobManager` (`job_manager.py`) tracks all subprocesses spawned by `run_subprocess`:
- Output is streamed into memory (capped at 10 MB per stream) and also written to per-job log files under `~/.neo/daemon/jobs/`.
- `cleanup_old_jobs()` removes **completed** jobs older than 24 h from the in-memory registry. Running jobs are never evicted regardless of age.
- `cleanup_old_jobs()` is called automatically by `BackendPoller` every hour — no manual intervention needed.

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

The hosted server (`mcpserver.heyneo.com`) is a **stateless bridge** — it translates MCP calls to Neo API requests but never runs daemons. It sets `NEO_NO_DAEMON=true` to skip daemon startup entirely.

**Execution always happens on the user's machine**, via:
- The Neo VS Code/Cursor extension (zero setup — handles everything automatically), OR
- The npm daemon (`npx neo-mcp-daemon`) — started automatically by the agent on first task submission

**Agent-driven daemon start (HTTP mode):**
When no daemon is found, `neo_submit_task` returns a `DAEMON_NOT_RUNNING` message with the exact startup command — `NEO_SECRET_KEY` pre-filled:
```
NEO_SECRET_KEY=sk-v1-... npx neo-mcp-daemon /path/to/workspace &
```
Agents with terminal access (Claude Code, Cursor, Windsurf, Codex CLI) ask user permission and run it. The user clicks **Allow** once — the npm daemon starts and tasks flow automatically. No manual terminal work required.

**VS Code extension users** don't need any of this — the extension manages the daemon automatically.

### stdio mode (local pip install / Docker on user's machine)

In stdio mode the server and daemon run on the same machine. `neo_submit_task` auto-starts `npx neo-mcp-daemon` silently if it's not running. No user action needed.

### Thread-ID based polling — the core loop
After submission, `init-chat-direct` returns a `thread_id`. **All status and message queries use `thread_id`** — these APIs work with API key auth:

- `GET /v2/thread/status/{thread_id}` → status (RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / etc.)
- `GET /v2/thread/thread-messages?thread_id=...` → messages array

`_poll_task_bg(thread_id)` runs as a background asyncio task, polling status every 3–60 s (adaptive ramp), fetching all messages on COMPLETED. Results land in `_active_polls[thread_id]` so `neo_task_status` and `neo_get_messages` return instantly from cache.

### Daemon registration and heartbeat
When the VS Code/Cursor extension daemon is running:
- `_register_with_daemon(deployment_id, secret_key)` reads `~/.neo/daemon/daemon.token` and POSTs to `http://127.0.0.1:31337/register`
- `_heartbeat_loop(deployment_id)` sends a keepalive every 60 s to prevent the daemon from evicting the registration

### Other design points
- `NEO_NO_DAEMON=true` skips all daemon startup — set automatically in Docker/bridge deployments.
- `NEO_READ_ONLY=true` strips all write tools at `list_tools()` time — only `neo_task_status`, `neo_get_messages` remain visible.
- `NEO_WORKSPACE_DIR` overrides `os.getcwd()` for `_server_cwd` — useful in Docker.
- `handle_error(status_code)` is the single error-mapping function; every tool calls it on non-200 responses.
- `neo_get_messages` paginates via `before=<timestamp>` cursor and hard-caps at 80 000 chars (~20 000 tokens).
- Thread ID is persisted to `~/.neo/active_thread_id` (`_THREAD_ID_FILE`) so follow-up tool calls can recover it without the caller re-supplying it.
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

## Test suite

Primary test file: `python/tests/test_concurrent_workspaces.py` (60 tests, no API key required).

```bash
python3 -m pytest python/tests/test_concurrent_workspaces.py -v
```

Coverage:

| Class | What it tests |
|---|---|
| `TestWriteCode` | relative/absolute/workdir paths, subdir creation, overwrite, unicode, traversal blocked, missing fields |
| `TestGetFileSecurity` | reads inside workspace, absolute path blocked/remapped, relative traversal blocked, missing file/field |
| `TestRemapToWorkspace` | all 4 container roots, deduplication, exact root match, nested paths, workdir hint, unknown root fallback |
| `TestListFiles` | basic listing, hidden files on/off, max_depth, missing directory, file count |
| `TestCreateSession` | with/without session_id, payload form |
| `TestUnknownAction` | unknown action, empty action, missing action |
| `TestConcurrentWorkspaceIsolation` | 3 threads × separate workspaces, concurrent `asyncio.gather`, 15 files across 3 threads, unknown thread fallback |
| `TestJobCleanup` | old completed removed, recent kept, running never evicted, empty registry, mixed, logs return None after cleanup |
| `TestWorkspaceRegistration` | default fallback, per-thread lookup, isolation, runtime registration, None thread_id |

When adding new features to `action_handlers.py`, `job_manager.py`, or `backend_poller.py` — add tests to this file.

## Known constraints

- Without the npm daemon running, tasks submit and track correctly via thread_id polling — but local file execution depends on the daemon being active.
- Do NOT send a fabricated `NEO_DEPLOYMENT_ID` unless it matches a real running sandbox — it causes a 30 s ReadTimeout.
- Workspace must always be the project/git root. Subdirectories cause duplicate nested folder creation (e.g. `test_2/test_2/`) due to path remapping logic.
- `test_server.py` tests an older monolithic server API and is no longer accurate — do not rely on it as a reference.

## Docker / CI

`Dockerfile` is in `python/`. The GitHub Actions workflow at `.github/workflows/publish-mcp.yml` builds and pushes to the internal ECR registry on every push to `main`. The public Docker image (`ghcr.io/heyneo/neo-mcp-server`) is maintained separately.

Docker security: container runs as non-root user (uid 1000), workspace is restricted to `/tmp/neo-workspace`, `NEO_NO_DAEMON=true` disables the local poller since the bridge server never has a daemon.

PyPI releases trigger automatically on `v*` version tags.
