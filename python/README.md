# neo-mcp (pip)

Python MCP server for [Neo](https://heyneo.so) — submit AI/ML tasks, poll status, read output, and control task lifecycle from any AI editor.

Install it, set your API key, register with your editor — that's it. Everything else is handled automatically.

Get your API key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

---

## Install

```bash
pip install neo-mcp
```

Requires Python 3.11+.

> **Tip:** use `pipx install neo-mcp` to install in an isolated environment and avoid conflicts with your project's virtualenv.

---

## Connecting to editors

Replace `sk-v1-YOUR_KEY` with your actual key.

---

### Claude Code

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
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
      "command": "neo-mcp",
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
      "command": "neo-mcp",
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
      "command": "neo-mcp",
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

### Continue.dev

`~/.continue/config.json`:

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

### OpenAI Codex CLI

`~/.codex/config.json`:

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

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `NEO_SECRET_KEY` | **Yes** | Your Neo API key (`sk-v1-...`) from [app.heyneo.so](https://app.heyneo.so) |
| `NEO_DEPLOYMENT_ID` | No | Pin a specific deployment UUID (auto-generated and persisted by default) |
| `NEO_WORKSPACE_DIR` | No | Override working directory (useful in Docker) |
| `NEO_READ_ONLY` | No | `true` — expose only status/message tools, disable submit/stop/pause |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `neo-mcp: command not found` | Re-run `pip install neo-mcp` and verify your PATH with `which neo-mcp` |
| Tools don't appear after registering | Open a **new session** — MCP tools load at session start, not mid-session |
| `Invalid API key` (401) | Re-check your key at app.heyneo.so → Settings → API Keys |
| `Trial or quota ended` (403) | Top up at the Neo dashboard |
| Task submitted but no files written | Daemon failed to start — run `neo-mcp doctor` to diagnose |
| Status stuck on RUNNING | Call `neo_task_status` to check; run `neo-mcp status` to inspect the daemon |
