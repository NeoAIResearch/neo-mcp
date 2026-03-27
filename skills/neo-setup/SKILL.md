---
name: neo-setup
description: Set up Neo MCP server authentication and daemon for a user. Use this skill when the user wants to install neo-mcp, configure their editor, start the daemon, or troubleshoot login/auth issues.
user-invocable: true
metadata: {"openclaw": {"emoji": "🔧", "os": ["darwin", "linux", "win32"]}}
---

# Neo Setup — Auth & Daemon Configuration

Use this skill to guide a user through setting up the Neo MCP server from scratch,
fixing auth issues, or starting the daemon.

---

## How Neo auth works

Neo MCP has two separate auth layers:

| Layer | Credential | Used for |
|---|---|---|
| MCP server | `NEO_SECRET_KEY` (`sk-v1-...`) | Task submission, status, messages — all thread APIs |
| Daemon | OAuth token (from `neo-mcp login`) | Polling `/v2/poll/{deployment_id}` to receive execution commands |

**The MCP server only needs the API key.** The daemon needs OAuth — but only because
the backend currently rejects API keys on the poll endpoint (a known issue, fix pending).

---

## Slash command: /neo-setup

When invoked as `/neo-setup`, walk the user through the setup flow below.

---

## Setup flow

### Step 1 — Install
```bash
pip install neo-mcp
```

### Step 2 — Set API key
```bash
export NEO_SECRET_KEY=sk-v1-your-key
```

### Step 3 — Login (required until backend fix ships)
```bash
neo-mcp login
```
Opens a browser login URL. On remote servers, the terminal displays a URL — the user
opens it on any device. Token is saved to `~/.neo/daemon/mcp_auth.json`.

### Step 4 — Start daemon
```bash
neo-mcp daemon
```
Starts a background process that polls Neo backend with the deployment UUID derived
from the API key. Daemon survives terminal close.

### Step 5 — Configure editor
```bash
# Claude Code (recommended — hosted server)
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-your-key" \
  --header "X-Neo-Deployment-Id: $(cat ~/.neo/daemon/standalone_deployment_id)"

# Or run the setup wizard (handles all of the above)
neo-mcp setup
```

---

## Troubleshooting

### "No healthy deployments available" (HTTP 400)
The daemon is not running or not registered for the deployment UUID.
```bash
neo-mcp daemon   # start it
# then retry the task
```

### Login fails on remote server
The localhost callback can't fire on a remote machine. The login flow uses a relay
through `mcpserver.heyneo.com` — user opens the printed URL on any browser.
If relay isn't deployed yet, use manual token paste fallback shown in the terminal.

### Daemon exits immediately
Check `mcp_auth.json` is valid:
```bash
cat ~/.neo/daemon/mcp_auth.json
# Should contain { "access_token": "...", ... }
# If access_token is "\" or empty, re-run neo-mcp login
```

### Auth token expired (401 from daemon)
```bash
neo-mcp login   # refreshes token in mcp_auth.json
neo-mcp daemon  # restart daemon with fresh token
```

---

## Pending backend changes (known issues)

These backend changes are in progress — once shipped, the setup flow simplifies:

| Change | Effect when shipped |
|---|---|
| Accept API key on `/v2/poll` | `neo-mcp login` no longer needed — daemon uses `NEO_SECRET_KEY` directly |
| Allow submission without deployment | No daemon needed for cloud tasks — zero setup |
| Whitelist relay domain on heyneo.so | `neo-mcp login` works seamlessly on remote servers |

See `docs/CHANGES.md` for full technical details on each change.

---

## One-command setup (after all backend changes ship)

```bash
pip install neo-mcp
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-your-key"
# Done. No login, no daemon, no deployment ID.
```
