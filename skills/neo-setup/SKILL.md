---
name: neo-setup
description: Set up Neo MCP server for a user. Use this skill when the user wants to install neo-mcp, configure their editor, or troubleshoot connection/auth issues.
user-invocable: true
metadata: {"openclaw": {"emoji": "🔧", "os": ["darwin", "linux", "win32"]}}
---

# Neo Setup

Use this skill to guide a user through installing and configuring the Neo MCP server.

---

## Slash command: /neo-setup

When invoked as `/neo-setup`, walk the user through the setup flow below.

---

## Setup — pip (recommended)

### Step 1 — Install

```bash
pip install neo-mcp
# or: pipx install neo-mcp
```

### Step 2 — Register with your editor

**Claude Code:**
```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
```

Open a **new Claude Code session** after running this.

**Cursor** — edit `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
    }
  }
}
```

**Windsurf** — edit `~/.codeium/windsurf/mcp_config.json`:
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
    }
  }
}
```

**VS Code** — edit `.vscode/mcp.json`:
```json
{
  "servers": {
    "neo": {
      "type": "stdio",
      "command": "neo-mcp",
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
    }
  }
}
```

### Step 3 — Submit your first task

The daemon auto-starts on the first task submission. No manual startup needed.

---

## Setup — npm (Node.js, no Python required)

### Step 1 — Install

```bash
npm install -g neo-mcp
```

### Step 2 — Register with your editor

**Claude Code:**
```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp-daemon --mcp
```

For other editors, use `neo-mcp-daemon` as the command with `args: ["--mcp"]`. See full editor configs at [docs/GUIDE.md](../../docs/GUIDE.md).

---

## Verifying the connection

```bash
claude mcp list   # should show neo with a green checkmark
```

Then in a new session ask: *"What Neo tools do you have available?"* — should list `neo_submit_task`, `neo_task_status`, etc.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `neo-mcp: command not found` | Re-run `pip install neo-mcp`, verify with `which neo-mcp` |
| `✗ Failed to connect` in `claude mcp list` | Run `claude mcp logs neo` — most likely `NEO_SECRET_KEY` not set |
| Neo tools don't appear | Open a **new session** — tools load at session start |
| `Invalid API key` (401) | Re-check key at [heyneo.com/dashboard](https://heyneo.com/dashboard?section=settings#access-keys) → Settings → API Keys |
| `No healthy deployments available` (400) | Daemon failed to auto-start — restart the MCP server |
| Files not written locally | Daemon stopped — check `neo-mcp status` and restart |

```bash
# Diagnostics (pip)
neo-mcp status
neo-mcp doctor
claude mcp logs neo
```
