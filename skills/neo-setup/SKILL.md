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

Neo MCP has two layers:

| Layer | Credential | Used for |
|---|---|---|
| MCP server | `NEO_SECRET_KEY` (`sk-v1-...`) | Task submission, status, messages — all thread APIs |
| Daemon | API key (pending) / OAuth token (current) | Polling the backend to receive execution commands |

**The MCP server only needs the API key.** The daemon currently needs OAuth — this is a known
limitation. Once the backend adds API key support to the poll endpoint (path TBD), `neo-mcp login`
becomes fully optional and the setup collapses to a single command.

**Zero-friction path (works today for agent users):** Agents with terminal access (Claude Code,
Cursor, Codex CLI, Windsurf) will automatically offer to run `neo-mcp daemon &` on first task
submission. The user just clicks yes. OAuth is still needed for the daemon until the backend change ships.

---

## Slash command: /neo-setup

When invoked as `/neo-setup`, walk the user through the setup flow below.

---

## Setup flow

### Step 1 — Add the MCP server (one command)
```bash
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-your-key"
```

Open a **new Claude Code session** after running this.

### Step 2 — Submit your first task

Your agent will detect no daemon is running and offer to start it:
```
Neo daemon needs to run locally. Can I start it?  [Yes / No]
```
Click **Yes** — the daemon starts and the task proceeds.

> **Note:** Until the backend adds API key support to the poll endpoint, the daemon needs
> a one-time OAuth login. If the daemon fails with an auth error, run:
> ```bash
> neo-mcp login   # opens browser — daemon starts automatically after login
> ```

### Alternative: VS Code/Cursor extension
Install the Neo extension and log in. It manages the daemon completely automatically — no manual steps, no CLI.

---

## Troubleshooting

### `DAEMON_NOT_RUNNING` on first task
Agent will offer to start the daemon — click yes. If running a web client (ChatGPT, Claude.ai):
```bash
neo-mcp login   # opens browser, daemon starts on success
```

### Daemon exits immediately / auth error
The poll endpoint requires OAuth until the backend change ships:
```bash
neo-mcp login   # refreshes token, daemon starts automatically after login
```

### `Failed to connect` in `claude mcp list`
Re-run `claude mcp add` — ensure the header is on a single line:
```bash
claude mcp add --scope user neo --transport http https://mcpserver.heyneo.com/mcp --header "Authorization: Bearer sk-v1-YOUR_KEY"
```

### `Invalid API key` (401)
Re-check your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

---

## Final state (after backend poll endpoint change ships)

```bash
# One command. Done. No login, no daemon, no deployment ID.
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-your-key"
```

Submit a task → agent auto-starts daemon → daemon polls with API key → everything works.
