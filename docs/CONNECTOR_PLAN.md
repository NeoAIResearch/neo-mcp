# Neo MCP — Claude Code Connector Export Plan

This document outlines how to expose neo-mcp as a proper Claude Code connector so users can add it directly from the Claude Code UI without manual CLI setup.

---

## Current State

The server runs over **stdio** transport — works perfectly for Claude Code and Cursor when installed locally via `pip install` or Docker. Not discoverable or installable from the Claude Code UI without manual `claude mcp add` commands.

---

## Target State

| Distribution method | Audience | Effort |
|---|---|---|
| **Remote HTTP connector** | Any Claude Code user, zero install | High |
| **Plugin bundle** | Claude Code users, one-click | Medium |
| **MCP Registry listing** | Discovery for HTTP connector | Low (after HTTP is done) |

---

## Phase 1 — Add HTTP Transport (Required for Remote Connector)

Claude Code UI only supports adding remote servers via HTTP or SSE URL. The current stdio transport must be joined by an HTTP mode.

### What to build

Add an HTTP/streamable transport mode toggled by `NEO_TRANSPORT=http`.

**New env vars:**
| Var | Default | Purpose |
|---|---|---|
| `NEO_TRANSPORT` | `stdio` | Set to `http` to run as remote server |
| `NEO_HTTP_PORT` | `8000` | Port to listen on |
| `NEO_HTTP_HOST` | `0.0.0.0` | Bind address |

### Implementation steps

1. Add `fastapi` + `uvicorn` (or use the MCP SDK's built-in streamable HTTP) to `requirements.txt`
2. In `server.py`, check `NEO_TRANSPORT` at startup:
   - `stdio` → existing `stdio_server()` path (unchanged)
   - `http` → mount the MCP app on an HTTP server using `mcp.server.fastmcp` or `mcp.server.http`
3. Update `Dockerfile` to expose port 8000 and accept `NEO_TRANSPORT=http`
4. Add health check endpoint `GET /health` for load balancers

### How users add it once hosted

```bash
claude mcp add --transport http neo https://your-hosted-url.com/mcp \
  --header "x-access-key: YOUR_NEO_API_KEY" \
  --header "Authorization: Bearer YOUR_NEO_SECRET_KEY"
```

Or from the Claude Code UI:
- Settings → MCP Servers → Add Remote Server
- Enter URL + auth headers

---

## Phase 2 — Host the Remote Server

The HTTP server needs a public HTTPS URL. Recommended options:

### Option A — Railway (simplest)
- Connect GitHub repo → auto-deploy on push to `main`
- Set env vars `NEO_TRANSPORT=http`, `NEO_API_KEY`, `NEO_SECRET_KEY` in Railway dashboard
- Railway provides HTTPS URL automatically

### Option B — Fly.io
```bash
fly launch --dockerfile Dockerfile
fly secrets set NEO_TRANSPORT=http NEO_API_KEY=... NEO_SECRET_KEY=...
fly deploy
```

### Option C — Self-hosted VPS (nginx + Docker)
```bash
docker run -d -p 8000:8000 \
  -e NEO_TRANSPORT=http \
  -e NEO_API_KEY=... \
  -e NEO_SECRET_KEY=... \
  ghcr.io/heyneo/neo-mcp-server
```
Put nginx in front for TLS termination.

### Multi-tenant consideration
If multiple users share one hosted instance, auth must be **per-request** (headers), not baked into the server at startup. Phase 3 (OAuth) handles this properly.

---

## Phase 3 — OAuth Support (For Public Multi-Tenant Connector)

For a public connector where each user authenticates with their own Neo account, implement OAuth 2.0:

1. **Auth server**: Neo backend (`master.heyneo.so`) issues OAuth tokens, or build a thin proxy that exchanges a Neo API key pair for a short-lived token
2. **MCP OAuth flow**: Claude Code opens a browser → user logs into Neo → token stored by Claude Code → sent as `Authorization: Bearer` on every MCP request
3. **Callback URL**: `https://claude.ai/api/mcp/auth_callback` (Claude handles token storage)

This makes the connector completely self-service — users just click "Connect" in Claude Code UI and log in with their Neo credentials.

---

## Phase 4 — MCP Registry Listing

Once the HTTP server is live, register it so it appears in Claude Code's server browser:

1. Submit to the MCP Registry: `https://api.anthropic.com/mcp-registry/docs`
2. Provide:
   - Server name: `neo`
   - Description: "Run AI/ML training, agents, RAG pipelines on Neo's remote compute backend"
   - URL: your hosted endpoint
   - Auth method: OAuth or header-based
   - Category: AI/ML

After approval, users can discover and add Neo directly from the Claude Code docs/UI server list.

---

## Phase 5 — Claude Code Plugin Bundle (Alternative Distribution)

A plugin lets users install neo-mcp with one command — no manual `claude mcp add` needed.

### Plugin structure
```
neo-claude-plugin/
├── plugin.json          # plugin manifest
├── .mcp.json            # bundled MCP server config
└── README.md
```

**plugin.json**
```json
{
  "name": "neo",
  "version": "1.0.0",
  "description": "Run AI/ML tasks on Neo's remote compute backend",
  "mcpServers": {
    "neo": {
      "type": "http",
      "url": "https://your-hosted-url.com/mcp"
    }
  }
}
```

### User install (one command)
```bash
/plugin install heyneo/neo-claude-plugin
```

That's it — no env vars, no `claude mcp add`, no Docker.

---

## Delivery Order

```
Phase 1 → Phase 2 → Phase 4 (for discovery)
                 ↘ Phase 3 (for public OAuth)
                 ↘ Phase 5 (for plugin install)
```

Phases 3 and 5 are independent and can be done in parallel after Phase 2.

---

## Files to create / modify

| File | Change |
|---|---|
| `src/neo_mcp/server.py` | Add HTTP transport branch |
| `requirements.txt` | Add `fastapi`, `uvicorn` (or MCP SDK HTTP deps) |
| `Dockerfile` | Expose port 8000, accept `NEO_TRANSPORT` |
| `.mcp.json` (new) | Project-scope config for teams |
| `neo-claude-plugin/plugin.json` (new) | Plugin manifest for Phase 5 |
| `docs/SETUP.md` | Add remote connector install instructions |
| `.github/workflows/publish-mcp.yml` | Add deploy step to Railway/Fly after image push |
