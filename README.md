# Neo MCP Server

Run AI/ML tasks on Neo's remote backend from any AI editor — Claude Code, Cursor, Windsurf, Zed, VS Code, Continue.dev, OpenAI Codex CLI, Claude.ai, and ChatGPT.

> **Task execution runs on your local machine** via the local Neo daemon. The daemon receives commands from Neo's backend and executes them locally — writing files, running scripts.

---

## Quick start

### Option A: pip install — stdio mode (RECOMMENDED — fully automatic, no manual daemon setup)

The MCP server runs locally on your machine. Daemon startup is 100% automatic — no agent cooperation needed, no terminal commands, nothing.

```bash
# On most systems:
pip install neo-mcp

# On Debian/Ubuntu servers (externally-managed Python):
pip install neo-mcp --break-system-packages
# or:
pip install neo-mcp --user
```

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
```

Open a **new Claude Code session**. On first task submission the server silently auto-starts the daemon:

1. `~/.neo/agent --daemon` (Go binary — preferred, fastest)
2. `npx --yes neo-mcp-daemon` (npm — auto-downloads and starts Go binary)
3. `neo-mcp daemon` (Python fallback)

You never have to touch the daemon yourself.

> **Prerequisite:** Node.js must be installed for the npm path (most servers have it).
> If not: `curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs`

---

### Option B: Hosted HTTP server (no install needed, works across editors)

```bash
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-YOUR_KEY"
```

Open a **new Claude Code session** and submit any AI/ML task.

**Important:** The hosted server is a stateless bridge — it cannot start the daemon for you. **Start the daemon yourself once before submitting any task:**

```bash
NEO_SECRET_KEY=sk-v1-YOUR_KEY npx --yes neo-mcp-daemon /path/to/your/workspace >/tmp/neo-daemon.log 2>&1 &
```

The daemon keeps running in the background. You only need to do this once per machine boot (or add it to your startup script).

> **Recommendation:** Use Option A (pip/stdio) on servers. HTTP mode works best with the VS Code/Cursor extension already installed — the extension manages the daemon automatically.

---

### Option C: VS Code or Cursor extension (zero setup)

Install the [Neo extension](https://marketplace.visualstudio.com/items?itemName=NeoResearch.neo) from the marketplace and log in — the extension manages the daemon automatically. Then add the MCP server with Option B above.

---

### Option D: Claude Code `/neo` skill

Install the skill so Claude Code knows to route AI/ML requests to Neo automatically:

```bash
curl -o ~/.claude/skills/neo.md \
  https://raw.githubusercontent.com/NeoResearchAI/MCPServer/main/skills/claude-code/SKILL.md
```

Then register the MCP server using any option above, and use `/neo <task>` in any Claude Code conversation.

> **Other agent frameworks:** see [`skills/`](skills/README.md) for Vercel AI SDK, OpenAI Agents SDK, and LangChain integration guides.

---

## Starting the daemon manually

If auto-start fails for any reason, run one of these in your terminal:

```bash
# Option 1 — Go binary (preferred, fastest)
NEO_SECRET_KEY=sk-v1-YOUR_KEY ~/.neo/agent --daemon >/tmp/neo-daemon.log 2>&1 &

# Option 2 — npm (installs Go binary automatically on first run)
NEO_SECRET_KEY=sk-v1-YOUR_KEY npx --yes neo-mcp-daemon /path/to/your/workspace >/tmp/neo-daemon.log 2>&1 &

# Option 3 — Python fallback (if npx unavailable)
NEO_SECRET_KEY=sk-v1-YOUR_KEY neo-mcp daemon
```

Check the daemon is running:
```bash
cat /tmp/neo-daemon.log
# or
cat ~/.neo/daemon/daemon.log
```

---

## What Neo is for

Neo runs AI/ML workloads on a remote backend so they don't block your local machine:

- Training or fine-tuning models (classification, regression, NLP, computer vision)
- Building AI agents and multi-agent workflows
- RAG pipelines and vector search systems
- LLM integrations and prompt engineering pipelines
- ML data preprocessing and feature engineering

For general coding your assistant works locally — it only routes to Neo for AI/ML tasks.

---

## Example session

```
You:       "Train a fraud detection model on fraud.csv, optimize for recall"
Assistant: Submitting to Neo…

           Step 1/4  Load and explore fraud.csv (50 000 rows, 23 features)
           Step 2/4  Engineer features + handle class imbalance (SMOTE)
           Step 3/4  Train XGBoost — AUC-ROC: 0.942, Recall: 0.91
           Step 4/4  Save fraud_model.pkl + evaluation_report.html

           Done. Files available via neo_get_files.
```

---

## How it works

```
neo_submit_task
      │
      ├─ stdio mode: auto-starts daemon if not running
      ├─ http mode: returns DAEMON_NOT_RUNNING if no daemon → agent runs startup command
      │
      ├─ POST /v2/thread/init-chat-direct  →  thread_id  (returns immediately)
      │         deployment_type: "vscode"
      │
      └─ background poller starts automatically
              │
              ├── GET /v2/thread/status/{thread_id}     (every 3–60 s, adaptive)
              │         status: RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / TERMINATED
              │
              └── GET /v2/thread/thread-messages         (fetched once on COMPLETED)
                        cached — neo_get_messages reads from cache instantly
```

All polling uses only `NEO_SECRET_KEY` — no OAuth required.

---

## Tools

| Tool | When to use |
|---|---|
| `neo_submit_task` | Start a task. Returns `thread_id` immediately. Use `wait_for_completion=true` for short tasks (< 3 min) to get output directly. |
| `neo_list_tasks` | Find running or recent tasks — use when you've closed a window or lost track of a task. Reconnects pollers automatically. |
| `neo_task_plan` | See live step-by-step progress with per-step status. Much cheaper than fetching full messages. Use while RUNNING. |
| `neo_task_status` | Quick overall status check: RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / PAUSED / TERMINATED. |
| `neo_get_messages` | Full conversation output once COMPLETED. Capped at ~20 000 tokens. |
| `neo_get_files` | Download files generated by a completed task (code, models, scripts). Returns contents inline. Available in stdio/local mode only. |
| `neo_send_feedback` | Reply when Neo asks a question (WAITING_FOR_FEEDBACK). |
| `neo_pause_task` | Pause a running task. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Stop and clean up a task. |

---

## Recommended workflow

**Short task (< 3 min):**
```
neo_submit_task  →  wait_for_completion=true  →  output returned directly
```

**Long task:**
```
neo_submit_task  →  thread_id
      ↓
neo_task_plan    →  live step progress (repeat until COMPLETED)
      ↓
neo_get_messages →  full output
neo_get_files    →  download any files
```

**Mid-task question:**
```
neo_task_status   →  WAITING_FOR_FEEDBACK
neo_send_feedback →  your reply  →  task resumes automatically
```

---

## All editors — setup snippets

### Claude Code

```bash
# pip (local stdio — most reliable, auto-starts daemon)
pip install neo-mcp
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-... \
  -- neo-mcp

# Hosted HTTP server (no install needed)
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."
```

> Open a **new Claude Code session** after running either command.
> Use a different API key per machine to avoid deployment-id collisions across devices.

### Cursor

`~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "neo": {
      "url": "https://mcpserver.heyneo.com/mcp",
      "headers": { "Authorization": "Bearer sk-v1-..." }
    }
  }
}
```

### Windsurf

`~/.codeium/windsurf/mcp_config.json`:
```json
{
  "mcpServers": {
    "neo": {
      "serverUrl": "https://mcpserver.heyneo.com/mcp",
      "headers": { "Authorization": "Bearer sk-v1-..." }
    }
  }
}
```

### VS Code (GitHub Copilot)

`.vscode/mcp.json`:
```json
{
  "servers": {
    "neo": {
      "type": "http",
      "url": "https://mcpserver.heyneo.com/mcp",
      "headers": { "Authorization": "Bearer sk-v1-..." }
    }
  }
}
```

### Zed

`~/.config/zed/settings.json`:
```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "npx",
        "args": ["-y", "mcp-remote", "https://mcpserver.heyneo.com/mcp",
                 "--header", "Authorization:Bearer sk-v1-..."]
      }
    }
  }
}
```

### Claude.ai (web)

1. Settings → Integrations → **Add custom connector**
2. URL: `https://mcpserver.heyneo.com/mcp`
3. Click **Connect** → enter your `sk-v1-...` key when prompted

### ChatGPT (web)

1. Settings → Connectors → **Add connector → Custom**
2. URL: `https://mcpserver.heyneo.com/mcp`
3. Click **Connect** → enter your `sk-v1-...` key when prompted

### Continue.dev

`~/.continue/config.json` (stdio only):
```json
{
  "mcpServers": [
    {
      "name": "neo",
      "transport": {
        "type": "stdio",
        "command": "neo-mcp",
        "env": { "NEO_SECRET_KEY": "sk-v1-..." }
      }
    }
  ]
}
```

### OpenAI Codex CLI

`~/.codex/config.json` (stdio only):
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": { "NEO_SECRET_KEY": "sk-v1-..." }
    }
  }
}
```

See [docs/CLIENTS.md](docs/CLIENTS.md) for the full guide including Docker, scope options, and transport support matrix.

---

## Configuration

| Variable | Required | Description |
|---|---|---|
| `NEO_SECRET_KEY` | **Yes** | Secret key (`sk-v1-...`) from [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys |
| `NEO_DEPLOYMENT_ID` | No | Pin a specific VS Code/Cursor extension sandbox ID (auto-discovered by default) |
| `NEO_READ_ONLY` | No | `true` = expose only status/plan/message tools (no submit, stop, pause) |
| `NEO_WORKSPACE_DIR` | No | Override working directory (useful in Docker) |
| `NEO_TRANSPORT` | No | `stdio` (default) or `http` |
| `NEO_PUBLIC_URL` | No | Override public base URL for OAuth discovery (default: `https://mcpserver.heyneo.com`) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Invalid API key` (401) | Re-check `NEO_SECRET_KEY` at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys |
| `Trial or quota ended` (403) | Top up at the Neo dashboard |
| `No healthy deployments available` (400) | No daemon running — see "Starting the daemon manually" above |
| Agent shows DAEMON_NOT_RUNNING but doesn't run a command | Run the daemon manually: `NEO_SECRET_KEY=sk-v1-... npx --yes neo-mcp-daemon /your/workspace &` |
| `~/.neo/agent` not found (Exit 127) | Go binary not installed — use `npx --yes neo-mcp-daemon` instead (installs it automatically) |
| `Task submitted but no files written locally` | Daemon not running — start it and resubmit |
| Task submission hangs or times out | Daemon stopped — restart with `npx --yes neo-mcp-daemon /workspace &` |
| `neo-mcp` not found | Re-run `pip install neo-mcp` and verify `which neo-mcp` |
| Neo tools don't appear after `claude mcp add` | Open a **new Claude Code session** — tools load at session start |
| Output truncated | Cap is ~20 000 tokens — use `neo_task_plan` for a concise step summary |
| Status stuck on RUNNING | Call `neo_task_plan` to see which step is blocked |
| `Failed to connect` in `claude mcp list` | Re-run `claude mcp add` with `--header "Authorization: Bearer YOUR_KEY"` on one line |
