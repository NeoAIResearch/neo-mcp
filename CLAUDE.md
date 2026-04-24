# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that wraps the Neo ML backend (`https://master.heyneo.so`). It exposes 12 tools to Claude Code so users can submit ML/AI tasks, poll status, read output, control task lifecycle, and register third-party credentials (GitHub, HuggingFace, Anthropic, OpenRouter) — via stdio transport.

Current pip version: **0.4.41**. Current npm version: **1.1.24**.

## Project structure

Each concern lives in its own top-level folder.

```
neo-mcp/
├── python/                         # pip-installable MCP server (neo-mcp package)
│   ├── src/neo_mcp/
│   │   ├── server.py               # MCP server — all 12 tools, single file
│   │   ├── integrations/           # GitHub/HF/Anthropic/OpenRouter credential storage
│   │   ├── oauth.py                # OAuth 2.0 PKCE authorization server (HTTP mode)
│   │   ├── setup.py                # setup wizard (neo-mcp setup)
│   │   ├── daemon.py               # Python daemon (fallback — primary is npm daemon)
│   │   └── login.py                # browser OAuth flow (neo-mcp login)
│   ├── tests/
│   │   ├── test_system.py                 # primary unit suite (127 tests, no key needed)
│   │   ├── test_auth.py                   # deployment ID policy tests
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
NEO_SECRET_KEY=sk-v1-your-secret npx --yes neo-mcp-daemon /path/to/workspace &

# Start the Python daemon (fallback if npm/npx not available)
NEO_SECRET_KEY=sk-v1-your-secret neo-mcp daemon

# Run unit tests (no key needed)
python3 -m pytest python/tests/ -v

# Run connectivity test (requires NEO_SECRET_KEY)
NEO_SECRET_KEY=sk-v1-... python3 python/tests/test_connection.py

# Register with Claude Code (pip stdio — Python poller runs in-process)
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=your-secret \
  -- neo-mcp

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
1. npm daemon already running (`npm_daemon.pid` alive) → skip Python poller
2. Python poller starts as a background asyncio task alongside the MCP server

Deployment UUID: both the npm daemon and the Python server generate a **random UUID on first run** and persist it to `~/.neo/daemon/standalone_deployment_id`. Each machine gets its own UUID regardless of which API key is used — preventing command-queue collision when the same key is used on multiple machines simultaneously. `get_or_create_deployment_id()` in `auth.py` reads the file (or creates it) on startup.

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

1. `get_or_create_deployment_id()` — reads `~/.neo/daemon/standalone_deployment_id` or creates it; falls back to `NEO_DEPLOYMENT_ID` env var
2. No separate daemon spawn — the Python `BackendPoller` is already running as a background asyncio task inside this same MCP-server process (started in `run_server`). If a standalone npm daemon is ALREADY running (PID file present), the Python poller defers to it; otherwise the Python poller handles all commands.
3. POSTs to `/v2/thread/init-chat-direct` with `deployment_type: "vscode"` and `deployment_id`
4. Returns `thread_id` immediately; background polling starts via `asyncio.create_task(_poll_task_bg(thread_id))`

**Important:** A `deployment_id` is **required** for Neo to route the task to an active daemon. Both the npm daemon and the Python server call `get_or_create_deployment_id()` which reads (or creates) `~/.neo/daemon/standalone_deployment_id` — a random UUID unique to this machine. The same file is read by both processes so they always agree on the deployment UUID.

### Deployment ID discovery
`get_or_create_deployment_id()` in `auth.py` (in priority order):
1. `NEO_DEPLOYMENT_ID` env var — explicit override
2. `~/.neo/daemon/standalone_deployment_id` — random UUID written on first run; shared by npm daemon and Python server
3. If file doesn't exist: generate `uuid.uuid4()`, write it, return it

### Execution — always local

**Execution always happens on the user's machine.** Polling options, in order of precedence:

1. **Neo VS Code / Cursor extension** — when the extension is installed and the user is working inside it, the extension's own daemon runs and this pip MCP server defers to it.
2. **Python `BackendPoller` (default for pip users)** — runs inline as a background asyncio task inside the same process as the MCP server. Starts automatically when `python3 -m neo_mcp` (or the `neo-mcp` console script) boots. Has the same adaptive polling as the npm daemon (`wait_time=1` active / `POLL_WAIT_TIME=5` idle). No user action needed.
3. **Standalone npm daemon (`npx neo-mcp-daemon`)** — optional; users who prefer a separate process can start it manually. When a live `npm_daemon.pid` is detected, the Python poller skips its own loop.

There is no auto-spawn of `npx neo-mcp-daemon` from `neo_submit_task` — the Python poller running in-process is all that's required for the pipeline to work end-to-end.

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
- `NEO_READ_ONLY=true` strips all write tools at `list_tools()` time — only `neo_task_status`, `neo_get_messages` remain visible.
- `NEO_WORKSPACE_DIR` overrides `os.getcwd()` for `_server_cwd` — useful in Docker.
- `handle_error(status_code)` is the single error-mapping function; every tool calls it on non-200 responses.
- `neo_get_messages` paginates via `before=<timestamp>` cursor and hard-caps at 80 000 chars (~20 000 tokens).
- Thread ID is persisted to `~/.neo/active_thread_id` (`_THREAD_ID_FILE`) so follow-up tool calls can recover it without the caller re-supplying it.
- Transport: `stdio_server` from `mcp.server.stdio` (stdio mode only).

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

### Integration tools (local only, no backend call)

| Tool | Purpose |
|---|---|
| `neo_list_integrations` | List configured third-party credentials (names only, no secrets) |
| `neo_add_integration` | Register a credential — writes to the native file (`~/.git-credentials`, `~/.cache/huggingface/token`) or `~/.neo/integrations/<provider>.env` with mode `0o600` |
| `neo_remove_integration` | Delete the credential file(s) and metadata entry |
| `neo_test_integration` | Probe the provider API with the stored credential |

Providers wired today: `github`, `huggingface`, `anthropic`, `openrouter`. Metadata file `~/.neo/integrations.json` is a shared contract with the VS Code extension. `IntegrationManager.env_for_subprocess()` is merged into every `run_subprocess` child env so Neo tasks inherit `ANTHROPIC_API_KEY`, `HF_TOKEN`, `GITHUB_TOKEN`, etc. without re-prompting.

### Secret storage backends (`integrations/secret_store.py`)

- **`file`** (default) — one `~/.neo/integrations/<provider>.env` file per provider at `0o600`. Works everywhere (headless Linux, Docker, CI). Readable by any same-user process.
- **`keyring`** (opt-in, `pip install neo-mcp[keyring]`) — OS keyring (macOS Keychain, Windows Credential Manager, Linux Secret Service). Encrypted at rest. Enable with `NEO_INTEGRATIONS_BACKEND=keyring`. Raises RuntimeError on startup if no functional backend — never silently falls back to plaintext.

Native tool-interface files (`~/.git-credentials`, `~/.cache/huggingface/token`) are always written so `git`/`huggingface-cli` pick up credentials regardless of backend — the SecretStore holds the canonical copy used by `load_env()` and `remove_secret()`.

## Test suite

**Python** — `python/tests/test_system.py` (127 tests, no API key required):

```bash
python3 -m pytest python/tests/test_system.py -v
```

**npm** — `npm/tests/system.test.ts` (139 tests, no API key required):

```bash
cd npm && npx vitest run tests/system.test.ts
```

Python coverage (16 test classes):

| Class | What it tests |
|---|---|
| `TestWriteCode` | relative/absolute/workdir paths, subdir creation, overwrite, unicode, traversal blocked, missing fields |
| `TestGetFile` | reads inside workspace, absolute path blocked/remapped, relative traversal blocked, missing file/field, roundtrip |
| `TestRunSubprocess` | detach, blocking stdout/stderr, nonzero exit, preflight check, terminate |
| `TestJobManager` | create, logs, completion polling, cleanup eviction, terminate running/unknown/completed |
| `TestListFiles` | basic listing, hidden files on/off, skip_dirs, max_depth, missing directory, file count, container path |
| `TestCreateSession` | explicit id, payload id, auto UUID |
| `TestDispatch` | unknown/empty/missing action, request_id echoed, all 7 actions routable |
| `TestRemapToWorkspace` | all 4 container roots, deduplication, exact root match, nested paths, workdir hint, unknown root fallback |
| `TestRemapCommandPaths` | ls/cat/python commands, multiple paths, non-container unchanged, cd-chained |
| `TestPathSecurity` | workspace/tmp allowed, /etc blocked, parent/sibling blocked |
| `TestSymlinkEscape` | write via symlink, absolute through symlink, get_file via symlink |
| `TestWorkspaceIsolation` | 3 threads × separate workspaces, concurrent asyncio.gather, container paths |
| `TestSafeSend` | first attempt, 2nd/3rd retry, all-3-fail no-raise, no-extra-calls |
| `TestThreadStatusGate` | TERMINATED/FAILED/STOPPED reject, RUNNING/PAUSED/unknown accept |
| `TestPollerWorkspaceRegistration` | in-memory update, overwrite, multiple threads isolated |
| `TestDeploymentId` | creates file, stable, not key-derived, env override, key-derived, different keys |

When adding new features to `action_handlers.py`, `job_manager.py`, or `backend_poller.py` — add tests to `test_system.py`.
When adding new features to `executor.ts` or `daemon.ts` — add tests to `npm/tests/system.test.ts`.

## Known constraints

- Without the npm daemon running, tasks submit and track correctly via thread_id polling — but local file execution depends on the daemon being active.
- Do NOT send a fabricated `NEO_DEPLOYMENT_ID` unless it matches a real running sandbox — it causes a 30 s ReadTimeout.
- Workspace must always be the project/git root. Subdirectories cause duplicate nested folder creation (e.g. `test_2/test_2/`) due to path remapping logic.
- `test_server.py` tests an older monolithic server API and is no longer accurate — do not rely on it as a reference.

## Docker / CI

`Dockerfile` is in `python/`. The GitHub Actions workflow at `.github/workflows/publish-mcp.yml` builds and pushes to the internal ECR registry on every push to `main`. The public Docker image (`ghcr.io/heyneo/neo-mcp-server`) is maintained separately.

Docker security: container runs as non-root user (uid 1000), workspace is restricted to `/tmp/neo-workspace`.

PyPI releases trigger automatically on `v*` version tags.
