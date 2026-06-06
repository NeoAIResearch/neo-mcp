# Neo MCP — Client Setup Guide

Get your secret key at [heyneo.com/dashboard](https://heyneo.com/dashboard?section=settings#access-keys) → Settings → API Keys.

---

## Quickstart

**pip install (recommended — daemon auto-starts silently):**
```bash
pip install neo-mcp
claude mcp add --scope user neo -e NEO_SECRET_KEY=sk-v1-YOUR_KEY -- neo-mcp
```

**npm daemon (required for local file execution):**
```bash
NEO_SECRET_KEY=sk-v1-YOUR_KEY npx --yes neo-mcp-daemon /path/to/your/workspace &
```

**Auto-configure all editors:**
```bash
pip install neo-mcp && neo-mcp setup
```

---

## Claude Code

```bash
# Install the MCP server
pip install neo-mcp   # or: pipx install neo-mcp

# Register with Claude Code
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- $(which neo-mcp)
```

Verify the server is connected:

```bash
claude mcp list
```

> **Important:** MCP tools load at session start. After running `claude mcp add`, start a **new Claude Code session** (new conversation) for the Neo tools to appear.

> **Scope options:** `--scope user` (global, recommended) · `--scope project` (writes `.mcp.json` in the current repo) · `--scope local` (this machine only)

---

## Cursor

Edit `~/.cursor/mcp.json` (create if it doesn't exist):

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Requires `pip install neo-mcp` first. Restart Cursor after editing.

**Alternative — npm daemon (Node.js required, more reliable for file-heavy tasks):**

```json
{
  "mcpServers": {
    "neo": {
      "command": "npx",
      "args": ["--yes", "neo-mcp-daemon", "--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

---

## Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Requires `pip install neo-mcp` first. Restart Windsurf after editing.

**Alternative — npm daemon (Node.js required, more reliable for file-heavy tasks):**

```json
{
  "mcpServers": {
    "neo": {
      "command": "npx",
      "args": ["--yes", "neo-mcp-daemon", "--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

---

## VS Code (GitHub Copilot)

Requires VS Code 1.99+. Edit `.vscode/mcp.json` in your workspace root (create if it doesn't exist):

```json
{
  "servers": {
    "neo": {
      "type": "stdio",
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Requires `pip install neo-mcp` first.

---

## Zed

Edit `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "neo-mcp",
        "args": [],
        "env": {
          "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
        }
      }
    }
  }
}
```

Requires `pip install neo-mcp` first.

---

## Continue.dev

Edit `~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "neo",
      "transport": {
        "type": "stdio",
        "command": "neo-mcp",
        "env": {
          "NEO_SECRET_KEY": "YOUR_SECRET_KEY"
        }
      }
    }
  ]
}
```

Requires `pip install neo-mcp` first.

---

## OpenAI Codex CLI

Edit `~/.codex/config.json`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "YOUR_SECRET_KEY"
      }
    }
  }
}
```

Requires `pip install neo-mcp` first.

---

## npm daemon — starting manually

The npm daemon is the primary execution engine. Start it manually if auto-start fails:

```bash
# Install and start (Node.js required)
npm install -g neo-mcp-daemon          # install globally once
NEO_SECRET_KEY=sk-v1-YOUR_KEY neo-mcp-daemon /path/to/your/workspace &

# Or use npx without installing globally
NEO_SECRET_KEY=sk-v1-YOUR_KEY npx --yes neo-mcp-daemon /path/to/your/workspace &
```

**To keep the daemon running across reboots**, add to `~/.zshrc` or `~/.bashrc`:
```bash
pgrep -f "neo-mcp-daemon" > /dev/null || \
  NEO_SECRET_KEY=sk-v1-YOUR_KEY npx --yes neo-mcp-daemon /your/workspace &
```

> **Node.js required.** Install with: `curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs`

---

## Python daemon — fallback

If Node.js is unavailable, the Python daemon is a fallback:

```bash
pip install neo-mcp
NEO_SECRET_KEY=sk-v1-YOUR_KEY neo-mcp daemon
```

The Python daemon starts automatically in stdio mode when no npm daemon is detected — no manual steps needed for pip-registered setups.

---

## How local execution works

**Files are always written to your local machine.** The Neo daemon runs as a background process on your machine, receives commands from Neo's backend, and writes files directly to your workspace directory.

Neo's backend uses `/app/project/` as its internal container path. When Neo's output mentions a path like `/app/project/src/model.py`, the actual file is at `<workspace>/src/model.py` on your machine. The daemon remaps these paths automatically — no action needed from you.

**Workspace is auto-detected.** The agent picks up the workspace from your current project context (git root or working directory) and passes it to `neo_submit_task` automatically. Files always land in the right place without you specifying a path.

---

## Setup wizard — flags reference

```bash
neo-mcp setup [flags]
```

| Flag | Description |
|------|-------------|
| `--secret-key KEY` | Neo secret key — skips interactive prompt |
| `--editor EDITORS` | Comma-separated: `claude,cursor,windsurf,zed,vscode,continue,codex` |
| `--scope SCOPE` | Claude Code scope: `user` (default), `project`, or `local` |
| `--no-backup` | Skip `.bak` backup files when overwriting existing configs |

**Examples:**

```bash
# Interactive — wizard prompts for key and editor selection
neo-mcp setup

# Non-interactive — configure Claude Code and Cursor
neo-mcp setup --secret-key sk-v1-... --editor claude,cursor

# Project-scoped Claude Code config
neo-mcp setup --secret-key sk-v1-... --editor claude --scope project
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Invalid API key` (401) | Wrong or missing secret key | Re-check your key at heyneo.com/dashboard → Settings → API Keys |
| `Trial or quota ended` (403) | Out of credits | Top up at the Neo dashboard |
| `neo-mcp` command not found | Install incomplete or PATH issue | Re-run `pip install neo-mcp`; verify with `which neo-mcp` |
| Tools don't appear after restart | Config path wrong or JSON syntax error | Validate the JSON and check the file location for your editor |
| `DAEMON_NOT_RUNNING` on task submit | No daemon active | Agent will offer to start it automatically — click yes. Or run manually: `NEO_SECRET_KEY=sk-v1-... npx --yes neo-mcp-daemon /your/workspace &` |
| Task submitted but no files written locally | Daemon not running | Agent will offer to start it — click yes |
| Status stuck on RUNNING | `sendResponse` network failure (pre-0.4.29 pip / pre-1.1.19 npm) | Upgrade: `pip install --upgrade neo-mcp` or `npm install -g neo-mcp@latest` |
| Status stuck on RUNNING | Step waiting for daemon | Call `neo_task_status` to see which step is blocked |
