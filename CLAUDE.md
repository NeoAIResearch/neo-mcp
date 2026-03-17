# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python MCP server that wraps the Neo ML backend (`https://master.heyneo.so`). It exposes 7 tools to Claude Code so users can submit ML tasks, poll status, read output, and control task lifecycle — all via stdio transport.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server directly (requires NEO_API_KEY)
NEO_API_KEY=your-key python server.py

# Test backend connectivity
NEO_API_KEY=your-key python test_connection.py

# Build Docker image
docker build -t neo-mcp-test .

# Run via Docker
docker run -i --rm -e NEO_API_KEY=your-key neo-mcp-test

# Register with Claude Code (Python)
claude mcp add --scope user neo -- python /absolute/path/to/server.py

# Register with Claude Code (Docker, after publish)
claude mcp add --scope user neo -- docker run -i --rm -e NEO_API_KEY ghcr.io/heyneo/neo-mcp-server

# View MCP server logs
claude mcp logs neo
```

## Architecture

**`server.py`** is the entire server — no submodules.

Key design points:
- `NEO_API_KEY` is validated at import time; missing key raises `ValueError` immediately.
- `NEO_READ_ONLY=true` strips all write tools at `list_tools()` time — only `neo_task_status` and `neo_get_messages` are registered.
- `handle_error(status_code)` is the single error-mapping function; every tool must call it on non-200 responses.
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

Auth header on every request: `x-access-key: $NEO_API_KEY`

## Docker / CI

`Dockerfile` is in this directory (`neo-mcp/`). The GitHub Actions workflow at `../.github/workflows/publish-mcp.yml` builds and pushes to `ghcr.io/heyneo/neo-mcp-server` on every push to `main` that touches `neo-mcp/`.
