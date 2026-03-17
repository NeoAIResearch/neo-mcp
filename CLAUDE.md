# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that wraps the Neo ML backend (`https://master.heyneo.so`). It exposes 7 tools to Claude Code so users can submit ML tasks, poll status, read output, and control task lifecycle — all via stdio transport.

## Project structure

```
neo-mcp/
├── src/neo_mcp/server.py   # MCP server — all 7 tools
├── docs/
│   ├── SETUP.md            # registration for all MCP clients
│   └── USAGE.md            # user guide + deployment steps
├── tests/test_connection.py
├── .github/workflows/publish-mcp.yml
├── Dockerfile
├── pyproject.toml
└── requirements.txt
```

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Set keys first (avoids split-line issues)
export NEO_API_KEY=your-access-key
export NEO_SECRET_KEY=your-secret-key

# Run the server directly
python3 src/neo_mcp/server.py

# Or after pip install:
neo-mcp

# Test backend connectivity
python3 tests/test_connection.py

# Build Docker image
docker build -t neo-mcp-test .

# Run via Docker
docker run -i --rm -e NEO_API_KEY=your-key -e NEO_SECRET_KEY=your-secret \
  -v ~/.neo:/root/.neo:ro neo-mcp-test

# Register with Claude Code (pip install)
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-key -e NEO_SECRET_KEY=your-secret \
  -- neo-mcp

# Register with Claude Code (Docker, after publish)
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-key -e NEO_SECRET_KEY=your-secret \
  -- docker run -i --rm -e NEO_API_KEY -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo:ro ghcr.io/heyneo/neo-mcp-server

# View MCP server logs
claude mcp logs neo
```

## Architecture

**`src/neo_mcp/server.py`** is the entire server — no submodules.

Key design points:
- `NEO_API_KEY` and `NEO_SECRET_KEY` are validated at startup in `_check_config()`; missing keys raise `ValueError` caught by `main()` which prints a clean error to stderr and exits 1.
- `NEO_READ_ONLY=true` strips all write tools at `list_tools()` time — only `neo_task_status` and `neo_get_messages` are registered.
- `NEO_WORKSPACE_DIR` overrides `os.getcwd()` for `_server_cwd` — useful when running inside Docker where CWD is always `/app`.
- `handle_error(status_code)` is the single error-mapping function; every tool must call it on non-200 responses.
- `neo_submit_task` polls internally (up to 80 × 15 s = 20 min) and returns the full result when done. No separate `neo_task_status` or `neo_get_messages` call needed for normal flows.
- `neo_get_messages` paginates using `before=<earliest message timestamp>` and hard-caps output at 80 000 characters (~20 000 tokens) to stay under Claude Code's output limit.
- Transport: `stdio_server` from `mcp.server.stdio` — no HTTP port needed.

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

Auth headers on every request: `Authorization: Bearer $NEO_SECRET_KEY` + `x-access-key: $NEO_API_KEY`

## Docker / CI

`Dockerfile` is in this directory (`neo-mcp/`). The GitHub Actions workflow at `../.github/workflows/publish-mcp.yml` builds and pushes to `ghcr.io/heyneo/neo-mcp-server` on every push to `main` that touches `neo-mcp/`.
