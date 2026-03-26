# Neo MCP — Docker Deployment Guide

---

## Two modes at a glance

| Mode | Who runs it | Where the key lives |
|------|-------------|---------------------|
| **stdio** | MCP client spawns the container as a subprocess | In the client's MCP config (`-e NEO_SECRET_KEY`) — passed to the process automatically |
| **HTTP** | You deploy it once; any client connects over the network | In each user's MCP client config (`Authorization: Bearer`) — sent as a header automatically |

**In both cases the user types their key exactly once — at client configuration time.
The server never stores or requires it.**

---

## stdio mode — local use (Claude Code / Cursor)

The client spawns the container, injects the key, and handles it from there.
The user never touches the key again after this one-time registration:

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-... \
  -- docker run -i --rm \
     -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo:ro \
     ghcr.io/heyneo/neo-mcp-server
```

The `-v ~/.neo:/root/.neo:ro` mount lets the container read VS Code/Cursor extension daemon data (sandbox ID, workspace mappings) from your host machine.

**After this command:** the key is stored in Claude Code's MCP config. Every subsequent invocation passes it automatically — no user action needed.

---

## HTTP mode — shared / self-hosted server

The server itself needs **no key at all**. Deploy it once; each user configures their own client with their own key.

### 1. Start the server (no key required)

```bash
docker run -d \
  --name neo-mcp \
  -e NEO_TRANSPORT=http \
  -p 8000:8000 \
  ghcr.io/heyneo/neo-mcp-server
```

Or with Docker Compose:

```yaml
services:
  neo-mcp:
    image: ghcr.io/heyneo/neo-mcp-server
    environment:
      NEO_TRANSPORT: http
    ports:
      - "8000:8000"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
```

```bash
docker compose up -d
```

### 2. Health check

```bash
curl http://localhost:8000/health
# {"status":"ok","server":"neo-mcp","transport":"http"}
```

### 3. Each user registers once in their client

The client stores the key and sends it automatically on every request — the user never has to enter it again.

**Claude Code:**
```bash
claude mcp add --transport http --scope user neo http://your-server:8000/mcp \
  --header "Authorization: Bearer sk-v1-..."
```

**Cursor / Windsurf** — `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "neo": {
      "url": "http://your-server:8000/mcp",
      "headers": { "Authorization": "Bearer sk-v1-..." }
    }
  }
}
```

---

## How auth works under the hood

```
stdio mode:
  claude mcp add -e NEO_SECRET_KEY=sk-v1-...
        │
        └─► stored in Claude Code's MCP config
              │
              └─► passed as env var to the container subprocess on every call
                    │
                    └─► server reads NEO_SECRET_KEY at startup, attaches to every Neo API request

HTTP mode:
  claude mcp add --header "Authorization: Bearer sk-v1-..."
        │
        └─► stored in Claude Code's MCP config
              │
              └─► sent as Authorization: Bearer header on every MCP request
                    │
                    └─► server extracts the key per-request, attaches to every Neo API request
```

Neither mode requires the user to do anything after the one-time setup.

---

## Build the image locally

```bash
# From the neo-mcp/ directory
docker build -t neo-mcp .
```

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NEO_TRANSPORT` | No | `stdio` (default) or `http` |
| `NEO_SECRET_KEY` | **stdio mode only** | Your `sk-v1-...` secret key. In HTTP mode the key comes from each client's `Authorization: Bearer` header — this env var is not used. |
| `NEO_HTTP_PORT` | No | Port to listen on in HTTP mode (default `8000`) |
| `NEO_HTTP_HOST` | No | Host to bind in HTTP mode (default `0.0.0.0`, pre-set in image) |
| `NEO_WORKSPACE_DIR` | No | Override the working directory reported to Neo |
| `NEO_DEPLOYMENT_ID` | No | Pin a specific VS Code extension sandbox ID |
| `NEO_READ_ONLY` | No | `true` = expose only status/plan/message tools |
| `NEO_PUBLIC_URL` | No | Base URL for OAuth discovery payloads (default `https://mcp.heyneo.so`) |

---

## Using the published image

```bash
docker pull ghcr.io/heyneo/neo-mcp-server:latest
```

The image is published automatically to GitHub Container Registry on every push to `main`. Specific versions are tagged by git SHA and semver (when a `v*` tag is pushed).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Container exits immediately in stdio mode | Normal — it exits after stdin closes. Use `-i` flag. |
| `NEO_SECRET_KEY not set` error | You are running in stdio mode without the key. Pass `-e NEO_SECRET_KEY=sk-v1-...` to `docker run`, or use `claude mcp add -e NEO_SECRET_KEY=...` to register it once. |
| Port 8000 already in use | Use `-p 8001:8000` to map to a different host port |
| `Invalid API key` (401) | The key in your client config is wrong. Double-check the value in your MCP client settings. |
| Tasks submit but no files appear locally | The VS Code/Cursor extension must be running on the host; mount `~/.neo` with `-v ~/.neo:/root/.neo:ro` |
| Output truncated | Cap is ~20 000 tokens — call `neo_task_plan` for a concise summary |
