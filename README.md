# Neo MCP

<!-- mcp-name: io.github.NeoAIResearch/neo-mcp -->

Connect your AI editor to Neo's remote execution backend. Describe an AI/ML task in plain language — Neo trains the model, builds the pipeline, or runs the workload on its backend, then writes all output files directly to your local machine.

Works with Claude Code, Cursor, Windsurf, VS Code (GitHub Copilot), Zed, Continue.dev, and OpenAI Codex CLI.

Get your API key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.

---

## How it works

```
Your editor  ──MCP──▶  neo-mcp server  ──API──▶  Neo backend
                            │                          │
                            └──────────────────▶  Local daemon
                                                  (writes files,
                                                   runs scripts)
```

1. You describe a task: *"Train a fraud detection model on data.csv"*
2. The editor calls `neo_submit_task` via MCP
3. Neo's backend processes the task and sends commands to the local daemon
4. The daemon runs on your machine — writing files, running scripts, installing packages
5. Output files appear directly in your workspace

**Files are always written to your machine, never stored remotely.**

Neo can also hold your third-party API keys (GitHub, HuggingFace, Anthropic, OpenRouter, OpenAI, AWS S3, Weights & Biases, Kaggle) locally so it can use them in tasks without re-prompting — keys stay on your machine, never sent to Neo's backend. Full guide: [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

---

## Install

Pick **pip** (Python 3.11+) or **npm** (Node.js 18+) — both work identically from the editor's perspective.

### pip

```bash
pip install neo-mcp
```

> Use `pipx install neo-mcp` to avoid virtualenv conflicts.

### npm

```bash
npm install -g neo-mcp
```

> Install Node.js 18+ if needed: `curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs`

---

## Connect to your editor

Replace `sk-v1-YOUR_KEY` with your actual API key. Pick the pip or npm command based on which you installed.

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

Open a **new Claude Code session** after running — tools load at session start, not mid-session.

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
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
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
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
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
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
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
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
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
- Or edit the file directly: `.vscode/mcp.json` in your workspace root (create if it doesn't exist)

**pip:**
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

**npm:**
```json
{
  "servers": {
    "neo": {
      "type": "stdio",
      "command": "neo-mcp-daemon",
      "args": ["--mcp"],
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
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
        "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
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
        "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
      }
    }
  }
}
```

Changes apply on save — no restart needed.

---

### Continue.dev

**Open the config:**
- `Ctrl+L` (VS Code) or `Ctrl+J` (JetBrains) to open the sidebar → click **Agent selector** above the chat input → click the **gear icon**
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
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
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
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
    }
  }
}
```

---

## Verify the connection

**Claude Code:**
```bash
claude mcp list
```

You should see `neo` with a green checkmark. Then open a new session and ask:

> "What Neo tools do you have available?"

The assistant should list `neo_submit_task`, `neo_task_status`, `neo_get_messages`, and more.

---

## Tools

| Tool | Description |
|---|---|
| `neo_submit_task` | Submit a task to Neo. Returns `thread_id` immediately. |
| `neo_list_tasks` | List all running and recent tasks — useful after reopening your editor. |
| `neo_task_status` | Check status: `RUNNING` / `COMPLETED` / `WAITING_FOR_FEEDBACK` / `PAUSED` / `TERMINATED`. |
| `neo_get_messages` | Read full task output once status is `COMPLETED`. Capped at ~20 000 tokens. |
| `neo_send_feedback` | Reply to Neo when it asks a clarifying question (`WAITING_FOR_FEEDBACK`). |
| `neo_pause_task` | Pause a running task. Can be resumed. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Permanently stop and clean up a task. |
| `neo_list_integrations` | List stored third-party API keys (names only — never the value). |
| `neo_add_integration` | Register a credential (GitHub, HuggingFace, Anthropic, OpenRouter, OpenAI, AWS S3, Weights & Biases, Kaggle) so Neo tasks can use it as an env var. |
| `neo_test_integration` | Call the provider's API to confirm a stored key is still valid. |
| `neo_remove_integration` | Delete a stored key from this machine. |

> **Integration tools** store credentials locally — file mode `0o600` under `~/.neo/integrations/` (or native tool files like `~/.aws/credentials`, `~/.netrc`, `~/.kaggle/kaggle.json`), or your OS keyring if `NEO_INTEGRATIONS_BACKEND=keyring`. Keys never leave your machine. Full guide: [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

---

## Workflow

**Standard (tasks over a few minutes):**
```
neo_submit_task  →  returns thread_id
      ↓
neo_task_status  →  poll until COMPLETED or WAITING_FOR_FEEDBACK
      ↓
neo_get_messages →  read full output
```

**Quick task:** Pass `wait_for_completion: true` to `neo_submit_task` — it blocks until done and returns output directly. No polling needed.

**Mid-task question:** When status is `WAITING_FOR_FEEDBACK`, call `neo_send_feedback` with your reply. Neo resumes automatically.

**Reconnecting after closing your editor:**
```
neo_list_tasks   →  all tasks with live status + thread IDs
neo_task_status  →  check the specific task you care about
neo_get_messages →  read output of any COMPLETED task
```

---

## Diagnostics (pip)

```bash
neo-mcp status      # daemon and key status
neo-mcp doctor      # full health check — identifies common issues
neo-mcp list        # list known threads
neo-mcp logs --source neo-mcp --lines 100   # MCP server logs
neo-mcp logs --source daemon --lines 100    # daemon logs

# JSON output
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
| `NEO_SECRET_KEY` | **Yes** | API key (`sk-v1-...`) from [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys |
| `NEO_DEPLOYMENT_ID` | No | Pin a specific daemon UUID (auto-generated by default) |
| `NEO_WORKSPACE_DIR` | No | Override workspace directory (useful in Docker or CI) |
| `NEO_READ_ONLY` | No | `true` = expose only status/message tools — disables submit, stop, and pause |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `neo-mcp: command not found` | Re-run `pip install neo-mcp`, verify with `which neo-mcp` |
| `neo-mcp-daemon: command not found` | Re-run `npm install -g neo-mcp`, verify with `which neo-mcp-daemon` |
| `✗ Failed to connect` in `claude mcp list` | Run `claude mcp logs neo` — most common cause is `NEO_SECRET_KEY` not set |
| Neo tools don't appear | Open a **new session** — tools load at session start, not mid-session |
| `Invalid API key` (401) | Re-check your key at app.heyneo.so → Settings → API Keys |
| `Trial or quota ended` (403) | Top up at the Neo dashboard |
| `No healthy deployments available` (400) | Daemon failed to auto-start — restart the MCP server and try again |
| Task submitted but no files written | Daemon stopped mid-task — check `neo-mcp status` and restart |
| Status stuck on `RUNNING` | Run `neo-mcp doctor` to diagnose; restart the MCP server |
| Output truncated | ~20 000 token cap — use `neo_task_status` for progress, `neo_get_messages` for final output only |

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

---

For a full setup guide including all editor options: [docs/GUIDE.md](docs/GUIDE.md)
