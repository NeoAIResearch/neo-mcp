# Neo MCP — Client Setup Guide

---

## What you need

A single **Neo secret key** (`sk-v1-...`) from the **Neo dashboard → Settings → API Keys**.

That's it — no second key, no OAuth token. Every connection method below uses this one key.

---

## Quickstart — setup wizard (recommended)

```bash
pip install neo-mcp
neo-mcp setup
```

The wizard detects your installed editors, prompts for your secret key, and writes all config files automatically. After setup, restart your editor and verify with `/mcp` (Claude Code) or the editor's MCP settings panel.

---

## Web browsers — no install required

> **Note:** The hosted endpoint (`https://mcp.heyneo.so/mcp`) is not yet deployed.
> Once it goes live, the web connector method below will work without any local install.
> For now, use the local pip or Docker method with your editor.

### Claude.ai _(coming soon — requires hosted endpoint)_

1. Open **claude.ai** → Settings → **Integrations**
2. Click **Add custom connector**
3. Enter the URL: `https://mcp.heyneo.so/mcp`
4. Complete the OAuth flow — enter your `sk-v1-...` key when prompted
5. Neo tools appear in every conversation automatically

Full walkthrough: [CONNECTORS.md](CONNECTORS.md)

### ChatGPT _(coming soon — requires hosted endpoint)_

1. Open **chatgpt.com** → Settings → **Connectors**
2. Click **Add connector → Custom**
3. Enter the URL: `https://mcp.heyneo.so/mcp`
4. Complete the OAuth flow — enter your `sk-v1-...` key when prompted

---

## Claude Code

### Remote — hosted endpoint _(coming soon)_

Once the hosted endpoint is live:

```bash
claude mcp add --transport http --scope user neo https://mcp.heyneo.so/mcp \
  --header "Authorization: Bearer YOUR_SECRET_KEY"
```

### Local — pip (works now)

```bash
pip install neo-mcp
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-... \
  -- neo-mcp
```

### Local — Docker (works now)

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-... \
  -- docker run -i --rm \
     -e NEO_SECRET_KEY \
     ghcr.io/heyneo/neo-mcp-server
```

> **Scope options:** `--scope user` applies globally across all projects; `--scope project` writes to `.mcp.json` in the current repo; `--scope local` is machine-local only.

---

## Cursor

Config file: `~/.cursor/mcp.json` (create if it doesn't exist; restart Cursor after editing)

### Remote _(coming soon)_

```json
{
  "mcpServers": {
    "neo": {
      "url": "https://mcp.heyneo.so/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SECRET_KEY"
      }
    }
  }
}
```

### Local — pip (works now)

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

---

## Windsurf

Config file: `~/.codeium/windsurf/mcp_config.json`

### Remote _(coming soon)_

```json
{
  "mcpServers": {
    "neo": {
      "serverUrl": "https://mcp.heyneo.so/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SECRET_KEY"
      }
    }
  }
}
```

### Local — pip (works now)

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

---

## Zed

Config file: `~/.config/zed/settings.json` — add under the `"context_servers"` key.

### Remote via mcp-remote proxy _(coming soon)_

Zed does not yet support HTTP MCP transport natively. `mcp-remote` bridges HTTP → stdio.

```bash
npm install -g mcp-remote   # one-time install
```

```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "npx",
        "args": [
          "-y", "mcp-remote",
          "https://mcp.heyneo.so/mcp",
          "--header", "Authorization:Bearer YOUR_SECRET_KEY"
        ]
      }
    }
  }
}
```

### Local — pip (works now)

```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "neo-mcp",
        "args": [],
        "env": {
          "NEO_SECRET_KEY": "sk-v1-..."
        }
      }
    }
  }
}
```

---

## VS Code (GitHub Copilot)

Requires VS Code 1.99+. Config file: `.vscode/mcp.json` in your workspace root (create if it doesn't exist).

### Remote _(coming soon)_

```json
{
  "servers": {
    "neo": {
      "type": "http",
      "url": "https://mcp.heyneo.so/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SECRET_KEY"
      }
    }
  }
}
```

### Local — pip (works now)

```json
{
  "servers": {
    "neo": {
      "type": "stdio",
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

---

## Continue.dev

Config file: `~/.continue/config.json` — add under `"mcpServers"` (Continue uses an array).

```json
{
  "mcpServers": [
    {
      "name": "neo",
      "transport": {
        "type": "stdio",
        "command": "neo-mcp",
        "env": {
          "NEO_SECRET_KEY": "sk-v1-..."
        }
      }
    }
  ]
}
```

> Continue.dev currently supports stdio transport only — remote option not available yet.

---

## OpenAI Codex CLI

Config file: `~/.codex/config.json`

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

---

## Setup wizard — flags reference

```bash
neo-mcp setup [flags]
```

| Flag | Description |
|------|-------------|
| `--secret-key KEY` | Neo secret key (`sk-v1-...`) — skips interactive prompt |
| `--editor EDITORS` | Comma-separated: `claude,cursor,windsurf,zed,vscode,continue,codex` |
| `--remote` | Write remote hosted configs (for when endpoint is live) instead of local stdio |
| `--scope SCOPE` | Claude Code scope: `user` (default), `project`, or `local` |
| `--no-backup` | Skip creating `.bak` backup files when overwriting existing configs |

**Examples:**

```bash
# Interactive — wizard prompts for key and editor selection
neo-mcp setup

# Non-interactive — configure Claude Code and Cursor, local
neo-mcp setup --secret-key sk-v1-... --editor claude,cursor

# Configure for project scope
neo-mcp setup --secret-key sk-v1-... --editor claude --scope project
```

---

## Transport support by editor

| Editor | Local stdio | Remote HTTP | Web OAuth |
|--------|-------------|-------------|-----------|
| Claude.ai | — | — | ⏳ when hosted |
| ChatGPT | — | — | ⏳ when hosted |
| Claude Code | ✅ now | ⏳ when hosted | — |
| Cursor | ✅ now | ⏳ when hosted | — |
| Windsurf | ✅ now | ⏳ when hosted | — |
| Zed | ✅ now | ⏳ when hosted (via `mcp-remote`) | — |
| VS Code Copilot | ✅ now | ⏳ when hosted | — |
| Continue.dev | ✅ now | ❌ stdio only | — |
| OpenAI Codex CLI | ✅ now | ❌ stdio only | — |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Invalid API key` (401) | Wrong or missing secret key | Re-check `NEO_SECRET_KEY` in your config |
| `Trial or quota ended` (403) | Out of credits | Top up at the Neo dashboard |
| `neo-mcp` command not found | Install incomplete or PATH issue | Re-run `pip install neo-mcp`; verify with `which neo-mcp` |
| Tools don't appear after restart | Config path wrong or JSON syntax error | Validate the JSON and check the file location for your editor |
| Task submitted but no files written locally | VS Code/Cursor extension not running | Start the Neo extension — it handles local file writes |
| Status stuck on RUNNING | Step waiting for daemon | Call `neo_task_plan` to see which step is blocked |
