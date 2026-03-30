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

Everything uses a single credential: `NEO_SECRET_KEY` (`sk-v1-...`).

The API key is used for all requests — task submission, status polling, messages, and daemon polling. No OAuth, no browser login, no separate daemon credential.

Agents with terminal access (Claude Code, Cursor, Codex CLI, Windsurf) automatically offer to start the daemon on first task submission — the user just clicks yes.

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
Click **Yes** — the daemon starts with your API key and the task proceeds.

### Alternative: VS Code/Cursor extension
Install the Neo extension and log in. It manages the daemon completely automatically — no manual steps, no CLI.

---

## Troubleshooting

### `DAEMON_NOT_RUNNING` on first task
Agent will offer to start the daemon — click yes. If running a web client (ChatGPT, Claude.ai), run manually:
```bash
NEO_SECRET_KEY=sk-v1-... neo-mcp daemon &
```

### Daemon exits immediately / auth error
Check that `NEO_SECRET_KEY` is set correctly:
```bash
echo $NEO_SECRET_KEY   # should print sk-v1-...
```

### `Failed to connect` in `claude mcp list`
Re-run `claude mcp add` — ensure the header is on a single line:
```bash
claude mcp add --scope user neo --transport http https://mcpserver.heyneo.com/mcp --header "Authorization: Bearer sk-v1-YOUR_KEY"
```

### `Invalid API key` (401)
Re-check your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

---

## Current state

```bash
# One command. Done. No login, no daemon startup needed manually.
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-your-key"
```

Submit a task → agent auto-starts daemon → daemon polls with `NEO_SECRET_KEY` → everything works.
