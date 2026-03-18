# Phase 1 — HTTP Transport: Progress Log

Goal: add `NEO_TRANSPORT=http` mode so neo-mcp can run as a remote server
reachable from the Claude Code UI (Settings → MCP Servers → Add Remote Server).

## Steps

- [x] **1. Audit available MCP SDK transports**
  - MCP SDK 1.26.0 installed — has `StreamableHTTPServerTransport` built in
  - No extra dependencies needed beyond `starlette` + `uvicorn`

- [x] **2. Add HTTP dependencies to `requirements.txt`**
  - Added `starlette>=0.40.0` and `uvicorn>=0.30.0`

- [x] **3. Add HTTP transport branch to `server.py`**
  - New env vars: `NEO_TRANSPORT` (default `stdio`), `NEO_HTTP_PORT` (default `8000`), `NEO_HTTP_HOST` (default `0.0.0.0`)
  - Per-request auth: reads `x-access-key` + `Authorization` headers so each user supplies their own Neo keys
  - Starlette app with routes: `POST /mcp`, `GET /mcp`, `DELETE /mcp`, `GET /health`
  - `stdio` path unchanged

- [x] **4. Update `Dockerfile`**
  - Expose port 8000
  - Pass `NEO_TRANSPORT` through to entrypoint

- [x] **5. Smoke-test HTTP mode locally**
  - `GET /health` → `{"status":"ok","server":"neo-mcp","transport":"http"}` ✓
  - `POST /mcp` with no auth → `401 Unauthorized` ✓
  - `POST /mcp` with correct Accept + auth headers → `200 OK` + MCP initialize response ✓
  - Instructions field visible in handshake response ✓

- [ ] **6. Push and verify CI builds Docker image**

## Env var reference (HTTP mode)

| Var | Default | Description |
|---|---|---|
| `NEO_TRANSPORT` | `stdio` | Set to `http` to enable HTTP mode |
| `NEO_HTTP_PORT` | `8000` | Port to listen on |
| `NEO_HTTP_HOST` | `0.0.0.0` | Bind address |
| `NEO_API_KEY` | — | Global fallback key (optional in HTTP mode) |
| `NEO_SECRET_KEY` | — | Global fallback secret (optional in HTTP mode) |

In HTTP mode, per-request headers take priority over env vars:
- `x-access-key: <NEO_API_KEY>`
- `Authorization: Bearer <NEO_SECRET_KEY>`

## How to add to Claude Code once hosted

```bash
claude mcp add --transport http neo https://your-host.com/mcp \
  --header "x-access-key: YOUR_NEO_API_KEY" \
  --header "Authorization: Bearer YOUR_NEO_SECRET_KEY"
```
