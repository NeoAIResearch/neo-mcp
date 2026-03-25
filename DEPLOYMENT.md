# Neo MCP — Docker Deployment Guide

---

## What this covers

Running Neo MCP as a Docker container in two modes:

| Mode | Use case |
|------|----------|
| **stdio** (default) | Local MCP client (Claude Code, Cursor, etc.) spawns the container as a subprocess |
| **HTTP** | Self-hosted endpoint that any MCP client connects to over the network |

---

## Prerequisites

- Docker installed and running
- Neo secret key (`sk-v1-...`) from the **Neo dashboard → Settings → API Keys**

---

## Quick start — stdio mode (for Claude Code / Cursor)

This is the standard way to use Neo MCP locally without installing Python.

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-... \
  -- docker run -i --rm \
     -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo:ro \
     ghcr.io/heyneo/neo-mcp-server
```

The `-v ~/.neo:/root/.neo:ro` mount lets the container read the VS Code/Cursor extension daemon data (sandbox ID, workspace mappings) from your host machine.

---

## Build the image locally

```bash
# From the neo-mcp/ directory
docker build -t neo-mcp .
```

The Dockerfile uses `python:3.11-slim`, installs all dependencies, and sets `neo-mcp` as the entrypoint.

---

## Run in stdio mode (test locally)

```bash
echo '{}' | docker run -i --rm \
  -e NEO_SECRET_KEY=sk-v1-... \
  -v ~/.neo:/root/.neo:ro \
  neo-mcp
```

Sending an empty JSON ping lets you confirm the container starts and the key is accepted.

---

## Run in HTTP mode (self-hosted endpoint)

HTTP mode exposes a persistent MCP server on port 8000. Useful for teams sharing one instance or for connecting web-based clients.

### Start the server

```bash
docker run -d \
  --name neo-mcp \
  -e NEO_SECRET_KEY=sk-v1-... \
  -e NEO_TRANSPORT=http \
  -p 8000:8000 \
  neo-mcp
```

### Health check

```bash
curl http://localhost:8000/health
# {"status":"ok","server":"neo-mcp","transport":"http"}
```

### Connect Claude Code to the HTTP endpoint

```bash
claude mcp add --transport http --scope user neo http://localhost:8000/mcp \
  --header "Authorization: Bearer sk-v1-..."
```

### Connect Cursor / Windsurf

`~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "neo": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer sk-v1-..." }
    }
  }
}
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NEO_SECRET_KEY` | — | **Required** — your `sk-v1-...` secret key |
| `NEO_TRANSPORT` | `stdio` | Set to `http` for HTTP server mode |
| `NEO_HTTP_PORT` | `8000` | Port to listen on in HTTP mode |
| `NEO_HTTP_HOST` | `0.0.0.0` | Host to bind in HTTP mode (pre-set in the image) |
| `NEO_WORKSPACE_DIR` | (CWD) | Override the working directory reported to Neo |
| `NEO_DEPLOYMENT_ID` | (auto) | Pin a specific VS Code extension sandbox ID |
| `NEO_READ_ONLY` | `false` | `true` = expose only status/plan/message tools |
| `NEO_PUBLIC_URL` | `https://mcp.heyneo.so` | Base URL for OAuth discovery payloads (HTTP mode) |

---

## Docker Compose (HTTP mode)

```yaml
services:
  neo-mcp:
    image: ghcr.io/heyneo/neo-mcp-server
    environment:
      NEO_SECRET_KEY: ${NEO_SECRET_KEY}
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

Start with:
```bash
NEO_SECRET_KEY=sk-v1-... docker compose up -d
```

---

## Using the published image

The image is published automatically to GitHub Container Registry on every push to `main`:

```bash
docker pull ghcr.io/heyneo/neo-mcp-server:latest
```

Specific versions are tagged by git SHA and semver (when a `v*` tag is pushed).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Container exits immediately in stdio mode | Normal — it exits after stdin closes. Use `-i` flag. |
| `NEO_SECRET_KEY not set` error | Pass `-e NEO_SECRET_KEY=sk-v1-...` to `docker run` |
| Port 8000 already in use | Use `-p 8001:8000` to map to a different host port |
| Tasks submit but no files appear locally | The VS Code/Cursor extension must be running on the host; mount `~/.neo` with `-v ~/.neo:/root/.neo:ro` |
| `Invalid API key` (401) | Double-check the secret key value |
| Output truncated | Cap is ~20 000 tokens — call `neo_task_plan` for a concise summary |
