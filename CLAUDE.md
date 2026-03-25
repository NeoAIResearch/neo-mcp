# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that wraps the Neo ML backend (`https://master.heyneo.so`). It exposes 9 tools to Claude Code so users can submit ML/AI tasks, poll status, read output, and control task lifecycle — all via stdio transport.

## Project structure

```
neo-mcp/
├── src/neo_mcp/server.py   # MCP server — all tools, single file
├── src/neo_mcp/oauth.py    # OAuth 2.0 PKCE authorization server (HTTP mode)
├── src/neo_mcp/setup.py    # setup wizard (neo-mcp setup)
├── docs/
│   ├── CLIENTS.md          # registration guide for all MCP clients
│   ├── USAGE.md            # user guide + deployment steps
│   ├── CONNECTORS.md       # web connector setup (Claude.ai + ChatGPT)
│   └── WEB_CONNECTOR.md    # web connector implementation notes
├── tests/
│   ├── test_connection.py
│   └── test_server.py
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

# Register with Claude Code (Docker, after publish)
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=your-secret \
  -- docker run -i --rm -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo:ro ghcr.io/heyneo/neo-mcp-server

# View MCP server logs
claude mcp logs neo
```

## Architecture

**`src/neo_mcp/server.py`** is the entire server — no submodules, ~730 lines.

### Auth
- Only `NEO_SECRET_KEY` is required (`sk-v1-...`) — passed as `Authorization: Bearer` on every request.
- `_check_config()` validates at startup; missing key prints a clean error to stderr and exits 1.

### Task submission — vscode vs cloud mode
`neo_submit_task` picks `deployment_type` via `_resolve_deployment(deployment_id)`:

| Transport | deployment_id available? | deployment_type |
|-----------|--------------------------|-----------------|
| stdio | any | `"vscode"` |
| http | yes | `"vscode"` |
| http | no (web connector) | `"cloud"` |

`NEO_DEPLOYMENT_TYPE=vscode|cloud` env var overrides this auto-detection.

- **vscode** routes execution to the local VS Code/Cursor extension daemon and includes the `deployment_id` + workspace directory prefix in the message.
- **cloud** runs on Neo's hosted backend — no `deployment_id` is sent and no local workspace prefix is added to the message (there is no local filesystem).

### Thread-ID based polling — the core loop
After submission, `init-chat-direct` returns a `thread_id`. **All status and message queries use `thread_id`** — these APIs work with API key auth:

- `GET /v2/thread/status/{thread_id}` → status (RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / etc.)
- `GET /v2/thread/thread-messages?thread_id=...` → messages array

`_poll_task_bg(thread_id)` runs as a background asyncio task, polling status every 3–15 s (ramping), fetching all messages on COMPLETED. Results land in `_active_polls[thread_id]` so `neo_task_status` and `neo_get_messages` return instantly from cache.

### Deployment ID discovery
`_get_deployment_id()` returns (in priority order):
1. `NEO_DEPLOYMENT_ID` env var — explicit pin
2. `_discover_sandbox_id()` — reads `~/.neo/daemon/daemon.log` (written by VS Code/Cursor extension) for the most recent `sandboxId` entry; falls back to `~/.neo/daemon/thread-workspaces.json`

### Other design points
- `NEO_READ_ONLY=true` strips all write tools at `list_tools()` time — only `neo_task_status` and `neo_get_messages` registered.
- `NEO_WORKSPACE_DIR` overrides `os.getcwd()` for `_server_cwd` — useful in Docker.
- `handle_error(status_code)` is the single error-mapping function; every tool calls it on non-200 responses.
- `neo_get_messages` paginates via `before=<timestamp>` cursor and hard-caps at 80 000 chars (~20 000 tokens).
- Thread ID is persisted to `~/.neo/active_thread_id` so follow-up tool calls can recover it without the caller re-supplying it.
- Transport: `stdio_server` from `mcp.server.stdio` — no HTTP port needed (HTTP mode also supported via `NEO_TRANSPORT=http`).

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
- `/v2/poll/{deployment_id}` — requires OAuth token (browser login), not API keys. This endpoint was previously used by an attempted built-in daemon; it was removed. The VS Code/Cursor extension handles that side of polling using its own OAuth token.
- Without the extension running, tasks still submit and track correctly — but local file execution depends on the extension's daemon being active.

## Docker / CI

`Dockerfile` is in this directory (`neo-mcp/`). The GitHub Actions workflow at `../.github/workflows/publish-mcp.yml` builds and pushes to `ghcr.io/heyneo/neo-mcp-server` on every push to `main` that touches `neo-mcp/`.
