# neo-mcp (npm)

Node.js MCP server for [Neo](https://heyneo.so) — submit AI/ML tasks, poll status, read output, and control task lifecycle from any AI editor. No Python required.

Install it, set your API key, register with your editor — that's it. Everything else is handled automatically.

Get your API key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

---

## Install

```bash
npm install -g neo-mcp
```

Requires Node.js 18+.

> **No global install needed:** you can use `npx --yes neo-mcp` and npm will download it on first run.

---

## Connecting to editors

Replace `sk-v1-YOUR_KEY` with your actual key.

---

### Claude Code

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp-daemon --mcp
```

Open a **new Claude Code session** after running this. Neo tools load at session start.

> **Scope options:** `--scope user` (global, recommended) · `--scope project` (writes `.mcp.json` in current repo) · `--scope local` (this machine only)

Verify it registered:
```bash
claude mcp list
```

---

### Cursor

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "npx",
      "args": ["--yes", "neo-mcp", "--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Restart Cursor after editing.

---

### Windsurf

`~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "npx",
      "args": ["--yes", "neo-mcp", "--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Restart Windsurf after editing.

---

### VS Code (GitHub Copilot)

`.vscode/mcp.json` in your workspace root (requires VS Code 1.99+):

```json
{
  "servers": {
    "neo": {
      "type": "stdio",
      "command": "npx",
      "args": ["--yes", "neo-mcp", "--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

---

### Zed

`~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "npx",
        "args": ["--yes", "neo-mcp", "--mcp"],
        "env": {
          "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
        }
      }
    }
  }
}
```

---

### Continue.dev

`~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "neo",
      "transport": {
        "type": "stdio",
        "command": "npx",
        "args": ["--yes", "neo-mcp", "--mcp"],
        "env": {
          "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
        }
      }
    }
  ]
}
```

---

### OpenAI Codex CLI

`~/.codex/config.json`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "npx",
      "args": ["--yes", "neo-mcp", "--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

---

## Tools

| Tool | Description |
|---|---|
| `neo_submit_task` | Submit an AI/ML task. Returns `thread_id` immediately. |
| `neo_list_tasks` | List running and recent tasks — reconnects pollers automatically. |
| `neo_task_status` | Check status: RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / PAUSED / TERMINATED. |
| `neo_get_messages` | Read full task output when COMPLETED. Capped at ~20 000 tokens. |
| `neo_send_feedback` | Reply when Neo asks a question (WAITING_FOR_FEEDBACK). |
| `neo_pause_task` | Pause a running task. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Stop and clean up a task permanently. |
| `neo_list_integrations` | List stored third-party API keys (names only — never the value). |
| `neo_add_integration` | Register a GitHub PAT / HuggingFace token / Anthropic key / OpenRouter key so Neo tasks can use it as an env var. |
| `neo_test_integration` | Call the provider's API to confirm a stored key is still valid. |
| `neo_remove_integration` | Delete a stored key from this machine. |

> **Integration tools** store credentials locally (file `0o600` under `~/.neo/integrations/`, or OS keyring with `NEO_INTEGRATIONS_BACKEND=keyring`). Keys never leave your machine. See the full guide at [docs/INTEGRATIONS.md](https://github.com/heyneo/neo-mcp/blob/main/docs/INTEGRATIONS.md).

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `NEO_SECRET_KEY` | **Yes** | Your Neo API key (`sk-v1-...`) from [app.heyneo.so](https://app.heyneo.so) |
| `NEO_DEPLOYMENT_ID` | No | Pin a specific deployment UUID (auto-generated and persisted by default) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools don't appear after registering | Open a **new session** — MCP tools load at session start, not mid-session |
| `Invalid API key` (401) | Re-check your key at app.heyneo.so → Settings → API Keys |
| `Trial or quota ended` (403) | Top up at the Neo dashboard |
| Task submitted but no files written | Ensure `NEO_SECRET_KEY` is set correctly and the project is open in your editor |
| Status stuck on RUNNING | Call `neo_task_status` to check current progress |
