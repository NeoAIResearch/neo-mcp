# Neo MCP — Client Setup Guide

**Neo MCP server:** `https://mcpserver.heyneo.com/mcp`

Get your secret key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

---

## Quickstart — choose a path

**VS Code/Cursor extension (simplest):** Install the Neo extension, log in, then add the MCP server:
```bash
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-YOUR_KEY"
```

**pip install (local, daemon auto-starts):**
```bash
pip install neo-mcp
claude mcp add --scope user neo -e NEO_SECRET_KEY=sk-v1-YOUR_KEY -- neo-mcp
```

**pip install (hosted server, no extension):**
```bash
pip install neo-mcp
NEO_SECRET_KEY=sk-v1-YOUR_KEY neo-mcp daemon &   # keep running
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-YOUR_KEY"
```

**Auto-configure all editors:**
```bash
pip install neo-mcp && neo-mcp setup
```

---

## Claude Code

```bash
claude mcp add --scope user neo --transport http https://mcpserver.heyneo.com/mcp --header "Authorization: Bearer YOUR_SECRET_KEY"
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
      "url": "https://mcpserver.heyneo.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SECRET_KEY"
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
      "serverUrl": "https://mcpserver.heyneo.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SECRET_KEY"
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
      "type": "http",
      "url": "https://mcpserver.heyneo.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SECRET_KEY"
      }
    }
  }
}
```

---

## Zed

Zed uses `npx mcp-remote` to bridge HTTP → stdio. Edit `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "npx",
        "args": ["-y", "mcp-remote", "https://mcpserver.heyneo.com/mcp", "--header", "Authorization:Bearer YOUR_SECRET_KEY"]
      }
    }
  }
}
```

---

## Claude.ai (web)

1. Open **claude.ai** → Settings → **Integrations**
2. Click **Add custom connector**
3. Enter URL: `https://mcpserver.heyneo.com/mcp`
4. Click **Connect** — you'll be redirected to a Neo authorization page
5. Enter your `sk-v1-...` key and click **Authorize**

Neo tools appear in every conversation automatically.

---

## ChatGPT (web)

1. Open **chatgpt.com** → Settings → **Connectors**
2. Click **Add connector → Custom**
3. Enter URL: `https://mcpserver.heyneo.com/mcp`
4. Click **Connect**, enter your `sk-v1-...` key, and click **Authorize**

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

Requires `pip install neo-mcp` first. Continue.dev supports stdio transport only.

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

## Local pip / Docker (alternative)

If you prefer running the server locally instead of using the hosted endpoint:

**pip:**
```bash
pip install neo-mcp
claude mcp add --scope user neo -e NEO_SECRET_KEY=YOUR_SECRET_KEY -- neo-mcp
```

**Docker:**
```bash
claude mcp add --scope user neo -e NEO_SECRET_KEY=YOUR_SECRET_KEY -- docker run -i --rm -e NEO_SECRET_KEY ghcr.io/heyneo/neo-mcp-server
```

---

## How local execution works

Tasks execute on your machine via a daemon process that polls the Neo backend for commands.

**Stdio (local pip install):** The daemon starts automatically when you submit your first task. No manual steps needed — `NEO_SECRET_KEY` is used for daemon auth.

**Hosted server (HTTP transport):** The hosted server can't start a daemon on your machine. Run it manually:
```bash
NEO_SECRET_KEY=sk-v1-YOUR_KEY neo-mcp daemon &
```

The daemon derives the same UUID from your API key as the hosted server does — no `--deployment-id` flag or header needed.

**To keep the daemon running across reboots**, add to `~/.zshrc` or `~/.bashrc`:
```bash
pgrep -f "neo-mcp daemon" > /dev/null || NEO_SECRET_KEY=sk-v1-YOUR_KEY neo-mcp daemon &
```

**Optional: browser login for OAuth-based auth (advanced):**
```bash
neo-mcp login    # opens browser → saves token to ~/.neo/daemon/mcp_auth.json
```
OAuth is not required — the daemon uses `NEO_SECRET_KEY` by default. Use `neo-mcp login` only if you want full OAuth with automatic token refresh.

---

## Setup wizard — flags reference

```bash
neo-mcp setup [flags]
```

| Flag | Description |
|------|-------------|
| `--secret-key KEY` | Neo secret key — skips interactive prompt |
| `--editor EDITORS` | Comma-separated: `claude,cursor,windsurf,zed,vscode,continue,codex` |
| `--remote` | Use the hosted server instead of local stdio |
| `--scope SCOPE` | Claude Code scope: `user` (default), `project`, or `local` |
| `--no-backup` | Skip `.bak` backup files when overwriting existing configs |

**Examples:**

```bash
# Interactive — wizard prompts for key and editor selection
neo-mcp setup

# Non-interactive — configure Claude Code and Cursor with hosted server
neo-mcp setup --secret-key sk-v1-... --editor claude,cursor --remote

# Project-scoped Claude Code config
neo-mcp setup --secret-key sk-v1-... --editor claude --scope project
```

---

## Transport support

| Editor | Remote HTTP (hosted) | Local stdio |
|--------|---------------------|-------------|
| Claude.ai | ✅ OAuth flow | — |
| ChatGPT | ✅ OAuth flow | — |
| Claude Code | ✅ `--transport http` | ✅ pip / Docker |
| Cursor | ✅ `url` + `headers` | ✅ `command` |
| Windsurf | ✅ `serverUrl` + `headers` | ✅ `command` |
| VS Code Copilot | ✅ `type: http` | ✅ `type: stdio` |
| Zed | ✅ via `mcp-remote` | ✅ `command` |
| Continue.dev | ❌ stdio only | ✅ `command` |
| OpenAI Codex CLI | ❌ stdio only | ✅ `command` |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Failed to connect` in `claude mcp list` | Header not passed or wrong key | Re-run `claude mcp add` with `--header "Authorization: Bearer YOUR_KEY"` on a single line |
| `Invalid API key` (401) | Wrong or missing secret key | Re-check your key at app.heyneo.so → Settings → API Keys |
| `Trial or quota ended` (403) | Out of credits | Top up at the Neo dashboard |
| `neo-mcp` command not found | Install incomplete or PATH issue | Re-run `pip install neo-mcp`; verify with `which neo-mcp` |
| Tools don't appear after restart | Config path wrong or JSON syntax error | Validate the JSON and check the file location for your editor |
| Task submitted but no files written locally | VS Code/Cursor extension not running | Start the Neo extension — it handles local file writes |
| Status stuck on RUNNING | Step waiting for daemon | Call `neo_task_plan` to see which step is blocked |
