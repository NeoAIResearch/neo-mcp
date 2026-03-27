# Neo MCP — pip Install Setup

Install neo-mcp and connect it to your AI editor. Choose your editor below.

> **Note:** The daemon currently requires the `/v2/poll` backend change to execute tasks locally. Until that ships, the extension-based workflow (VS Code/Cursor) is the recommended path for local file execution.

---

## Install

```bash
pip install neo-mcp
```

Get your API key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

---

## Claude Code

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
```

Then open a **new Claude Code session** — tools load at session start.

Verify it connected:
```bash
claude mcp list
```

> **Scope options:** `--scope user` (global, all projects) · `--scope project` (writes `.mcp.json` in current repo) · `--scope local` (this machine only)

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

Restart Cursor.

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

Restart Windsurf.

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
          "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
        }
      }
    }
  ]
}
```

---

## OpenAI Codex CLI

Edit `~/.codex/config.json`:

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

---

## Docker (alternative to pip)

If you prefer not to install Python packages, use Docker instead:

**Claude Code:**
```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- docker run -i --rm \
     -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo \
     ghcr.io/heyneo/neo-mcp-server
```

**Cursor / Windsurf / Continue.dev / Codex CLI** — use `command` + `args`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "NEO_SECRET_KEY",
        "-v", "~/.neo:/root/.neo",
        "ghcr.io/heyneo/neo-mcp-server"
      ],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

---

## How the daemon starts

In stdio mode, neo-mcp auto-starts a local daemon when you submit your first task. The daemon:

- Derives a UUID from your API key: `SHA-256(NEO_SECRET_KEY)[:16]`
- Polls the Neo backend for execution commands
- Runs Python scripts and bash commands on your machine
- Writes output files directly to your project directory

No manual daemon startup needed — it runs in the background automatically.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `neo-mcp: command not found` | Re-run `pip install neo-mcp` and verify `which neo-mcp` |
| `Invalid API key` (401) | Re-check your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys |
| `Trial or quota ended` (403) | Top up at the Neo dashboard |
| Neo tools don't appear after setup | Open a **new editor session** — tools load at startup |
| Task submitted but no files written | Daemon is running but backend change pending — use VS Code/Cursor extension in the meantime |
| `No healthy deployments available` | Daemon failed to start — check `~/.neo/daemon/daemon.log` |
