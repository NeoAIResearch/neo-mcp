# Neo MCP — Complete Setup & Usage Guide

Neo MCP connects your AI editor to Neo's remote execution backend. Submit AI/ML tasks, track progress, and receive output files — all written directly to your local machine.

**All you need:** a Neo API key and either Python or Node.js.

Get your API key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

---

## Table of Contents

1. [How it works](#how-it-works)
2. [Choosing your install method](#choosing-your-install-method)
3. [Install — pip (Python)](#install--pip-python)
4. [Install — npm (Node.js)](#install--npm-nodejs)
5. [Connecting to editors](#connecting-to-editors)
   - [Claude Code](#claude-code)
   - [Cursor](#cursor)
   - [Windsurf](#windsurf)
   - [VS Code (GitHub Copilot)](#vs-code-github-copilot)
   - [Zed](#zed)
   - [Continue.dev](#continuedev)
   - [OpenAI Codex CLI](#openai-codex-cli)
6. [Verifying the connection](#verifying-the-connection)
7. [Tools reference](#tools-reference)
8. [Workflows](#workflows)
9. [Diagnostics](#diagnostics)
10. [Environment variables](#environment-variables)
11. [Troubleshooting](#troubleshooting)

---

## How it works

```
Your editor  ──MCP──▶  neo-mcp server  ──API──▶  Neo backend
                            │                          │
                            │                    routes commands
                            │                          │
                            └──────────────────▶  Local daemon
                                                  (writes files,
                                                   runs scripts)
```

1. You describe a task in your editor ("train a fraud detection model on data.csv")
2. The editor calls `neo_submit_task` via the MCP server
3. Neo's backend processes the task and sends execution commands to the local daemon
4. The daemon runs on your machine — writing files, executing scripts, installing packages
5. Output files appear directly in your local workspace

**Files are always written to your machine, never stored remotely.**

---

## Choosing your install method

| | pip (Python) | npm (Node.js) |
|---|---|---|
| Runtime required | Python 3.11+ | Node.js 18+ |
| MCP server command | `neo-mcp` | `neo-mcp-daemon --mcp` |
| Daemon | Auto-starts on first task | Built into MCP server process |
| Best for | Python-first environments | Node.js environments, no Python |

Both packages are named `neo-mcp` — one on PyPI, one on npm.

---

## Install — pip (Python)

```bash
pip install neo-mcp
```

> Use `pipx install neo-mcp` to avoid virtualenv conflicts.

The MCP server (`neo-mcp`) auto-starts the local daemon when you submit your first task. No manual daemon management needed.

---

## Install — npm (Node.js)

```bash
npm install -g neo-mcp
```

> Requires Node.js 18+. Install with: `curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs`

The `neo-mcp-daemon --mcp` command runs the MCP server and daemon together in a single process.

---

## Connecting to editors

For each editor, pick **pip** or **npm** based on which you installed. Replace `sk-v1-YOUR_KEY` with your actual API key.

---

### Claude Code

**pip:**
```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
```

**npm:**
```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp-daemon --mcp
```

After running either command, open a **new Claude Code session** — MCP tools load at session start, not mid-session.

> **Scope options:**
> - `--scope user` — global, applies to all projects (recommended)
> - `--scope project` — writes `.mcp.json` in the current repo
> - `--scope local` — this machine only

---

### Cursor

**Open the config:**
- GUI: `Ctrl+Shift+J` (Windows/Linux) or `Cmd+Shift+J` (Mac) → **Tools & MCP** → **New MCP Server**
- Or edit the file directly: `~/.cursor/mcp.json`

**pip:**
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

**npm:**
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp-daemon",
      "args": ["--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Restart Cursor after editing the file directly. Changes via the GUI apply immediately.

---

### Windsurf

**Open the config:**
- GUI: `Ctrl+,` (Windows/Linux) or `Cmd+,` (Mac) → **Cascade** → **Plugins (MCP servers)** → **Manage Plugins** → **View raw config**
- Or edit the file directly: `~/.codeium/windsurf/mcp_config.json`

**pip:**
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

**npm:**
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp-daemon",
      "args": ["--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Changes apply on save — no restart needed.

---

### VS Code (GitHub Copilot)

Requires VS Code 1.99+.

**Open the config:**
- Command Palette: `Ctrl+Shift+P` (Windows/Linux) or `Cmd+Shift+P` (Mac) → **"Chat: Open Chat Customizations"**
- Or edit the file directly: `.vscode/mcp.json` in your workspace root (create it if it doesn't exist)

**pip:**
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

**npm:**
```json
{
  "servers": {
    "neo": {
      "type": "stdio",
      "command": "neo-mcp-daemon",
      "args": ["--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

> MCP tools only work in **Copilot Agent mode** — switch to Agent mode in the chat panel.

---

### Zed

**Open the config:**
- `Ctrl+Alt+,` (Windows/Linux) or `Cmd+Alt+,` (Mac) — opens `settings.json` directly
- Or: Command Palette → **"zed: open settings"**

Add inside `~/.config/zed/settings.json`:

**pip:**
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

**npm:**
```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "neo-mcp-daemon",
        "args": ["--mcp"],
        "env": {
          "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
        }
      }
    }
  }
}
```

Changes apply on save — no restart needed.

---

### Continue.dev

**Open the config:**
- `Ctrl+L` (VS Code) or `Ctrl+J` (JetBrains) to open the sidebar → click **Agent selector** above the chat input → **gear icon**
- Or edit the file directly: `~/.continue/config.yaml`

**pip:**
```yaml
mcpServers:
  - name: neo
    command: neo-mcp
    env:
      NEO_SECRET_KEY: sk-v1-YOUR_KEY
```

**npm:**
```yaml
mcpServers:
  - name: neo
    command: neo-mcp-daemon
    args:
      - --mcp
    env:
      NEO_SECRET_KEY: sk-v1-YOUR_KEY
```

> MCP tools only work in **Agent mode** — switch to Agent in the mode selector.

---

### OpenAI Codex CLI

**Open the config:**
- Run `codex mcp` to manage servers interactively via CLI
- Or edit the file directly: `~/.codex/config.json`

**pip:**
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

**npm:**
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp-daemon",
      "args": ["--mcp"],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

---

## Verifying the connection

**Claude Code:**
```bash
claude mcp list
```

You should see `neo` with a green checkmark. If it shows `✗ Failed to connect`, see [Troubleshooting](#troubleshooting).

**Test the tools are loaded** — in a new session, ask:
> "What Neo tools do you have available?"

The assistant should list `neo_submit_task`, `neo_task_status`, `neo_get_messages`, and the rest.

---

## Tools reference

| Tool | Description |
|---|---|
| `neo_submit_task` | Submit an AI/ML task to Neo. Returns `thread_id` immediately. |
| `neo_list_tasks` | List all running and recent tasks. Useful after closing and reopening your editor. |
| `neo_task_status` | Check task status: `RUNNING` / `COMPLETED` / `WAITING_FOR_FEEDBACK` / `PAUSED` / `TERMINATED`. |
| `neo_get_messages` | Read the full task output once status is `COMPLETED`. Capped at ~20 000 tokens. |
| `neo_send_feedback` | Reply to Neo when it asks a clarifying question (`WAITING_FOR_FEEDBACK`). |
| `neo_pause_task` | Pause a running task. Can be resumed. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Permanently stop and clean up a task. |

---

## Workflows

### Standard workflow (tasks over 3 minutes)

```
neo_submit_task   →  returns thread_id immediately
       ↓
neo_task_status   →  poll until COMPLETED or WAITING_FOR_FEEDBACK
       ↓
  WAITING_FOR_FEEDBACK?  →  neo_send_feedback  →  loop back to status
       ↓
  COMPLETED  →  neo_get_messages  →  read full output
```

### Quick task (under 3 minutes)

Pass `wait_for_completion: true` to `neo_submit_task` — it blocks until done and returns the full output directly. No polling needed.

### Mid-task question

When Neo needs clarification it pauses and sets status to `WAITING_FOR_FEEDBACK`. Reply naturally:

> "Tell Neo to use XGBoost and target the 'churned' column"

The assistant calls `neo_send_feedback` and Neo resumes automatically.

### Reconnecting after closing your editor

```
neo_list_tasks   →  see all tasks with live status + thread IDs
neo_task_status  →  check the specific task you care about
neo_get_messages →  read output of any COMPLETED task
```

---

## Diagnostics

These commands work with the pip package:

```bash
# Check daemon and key status
neo-mcp status

# Full health check — identifies common issues
neo-mcp doctor

# List known threads
neo-mcp list

# View MCP server logs
neo-mcp logs --source neo-mcp --lines 100

# View daemon logs
neo-mcp logs --source daemon --lines 100

# JSON output for scripting
neo-mcp status --json
neo-mcp doctor --json
```

**Claude Code logs:**
```bash
claude mcp logs neo
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `NEO_SECRET_KEY` | **Yes** | Your API key (`sk-v1-...`) from [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys |
| `NEO_DEPLOYMENT_ID` | No | Pin a specific daemon UUID. Auto-generated and persisted to `~/.neo/daemon/standalone_deployment_id` by default. |
| `NEO_WORKSPACE_DIR` | No | Override the default workspace directory (useful in Docker or CI). |
| `NEO_READ_ONLY` | No | Set to `true` to expose only status/message tools — disables submit, stop, and pause. |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `neo-mcp: command not found` | pip install didn't complete or PATH issue | Re-run `pip install neo-mcp` and verify with `which neo-mcp` |
| `neo-mcp-daemon: command not found` | npm install didn't complete or PATH issue | Re-run `npm install -g neo-mcp` and verify with `which neo-mcp-daemon` |
| `✗ Failed to connect` in `claude mcp list` | MCP server crashing on startup | Run `claude mcp logs neo` to see the error. Most common cause: `NEO_SECRET_KEY` not set. |
| Neo tools don't appear after adding the server | Session not restarted | Open a **new session** — tools load at session start, not mid-session |
| `neo` being confused with Neovim or a CLI tool | MCP tools not loaded | The MCP server isn't connecting — check `claude mcp list` and `claude mcp logs neo` |
| `Invalid API key` (401) | Wrong or expired key | Re-check your key at app.heyneo.so → Settings → API Keys |
| `Trial or quota ended` (403) | Out of credits | Top up at the Neo dashboard |
| `No healthy deployments available` (400) | No daemon running | Daemon failed to auto-start — re-run `neo-mcp` (pip) or `neo-mcp-daemon --mcp` (npm) and try again |
| Task submitted but no files written locally | Daemon stopped or crashed | Check `neo-mcp status` — restart if not running |
| Status stuck on `RUNNING` for a long time | Daemon crashed mid-task | Run `neo-mcp doctor` to diagnose; restart the MCP server |
| `neo-mcp --mcp` fails when both pip and npm are installed | pip's `neo-mcp` binary shadows npm's | Use `neo-mcp-daemon --mcp` for the npm path — that binary name is npm-only |
| Output truncated | 20 000 token cap in `neo_get_messages` | Use `neo_task_status` for progress checks; `neo_get_messages` for final output only |

---

## Example prompts

```
Train a fraud detection model on fraud.csv, optimize for recall
```

```
Build a sentiment analysis pipeline for product reviews and save the model
```

```
Analyse sales_data.csv and produce a feature importance report
```

```
Fine-tune a text classifier on my training data with 5-fold cross-validation
```

Neo handles the ML execution — your editor handles everything else.
