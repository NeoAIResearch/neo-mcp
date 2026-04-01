# Neo MCP Server

Run AI/ML tasks on Neo's remote backend from any AI editor — Claude Code, Cursor, Windsurf, Zed, VS Code, Continue.dev, OpenAI Codex CLI, Claude.ai, and ChatGPT.

> **Task execution runs on your local machine** via the local Neo daemon. The daemon receives commands from Neo's backend and executes them locally — writing files, running scripts.

---

## Quick start

### Step 1 — Install the daemon (required for file execution)

The Neo daemon is a small background process that runs on your machine. It receives commands from Neo's backend and executes them locally — writing files, running scripts, installing packages. **Without it, tasks submit and track correctly but no files are ever written to your machine.**

Pick whichever install method matches your environment:

---

#### Option 1: curl — recommended for servers and bare VMs

No prerequisites. Works on any Linux/Mac machine where `curl` is available.

```bash
mkdir -p ~/.neo \
  && curl -sSL https://heyneo.so/download/agent -o ~/.neo/agent \
  && chmod +x ~/.neo/agent
```

Start the daemon:

```bash
NEO_SECRET_KEY=sk-v1-YOUR_KEY ~/.neo/agent --daemon >/tmp/neo-daemon.log 2>&1 &
```

> This is the most reliable path. The Go binary has no runtime dependencies — no Node.js, no Python, nothing else required.

---

#### Option 2: npm — for machines with Node.js installed

```bash
NEO_SECRET_KEY=sk-v1-YOUR_KEY npx --yes neo-mcp-daemon /path/to/your/workspace >/tmp/neo-daemon.log 2>&1 &
```

On first run this downloads and installs the Go binary to `~/.neo/agent` automatically, then starts it. Subsequent runs use the cached binary.

> Requires Node.js. If not installed: `curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs`

---

#### Option 3: pip — for machines with Python installed

Install the MCP server package (which includes the Python daemon as a fallback):

```bash
pip install neo-mcp        # most systems
pip install neo-mcp --user  # if you get permissions errors
```

Start the daemon:

```bash
NEO_SECRET_KEY=sk-v1-YOUR_KEY neo-mcp daemon
```

> Requires Python 3.9+. The pip path is most common on developer laptops where Python is already present.

---

> **Why not let the agent start the daemon automatically?**
> The MCP server can instruct the agent to run a startup command on your behalf. However, **agent execution of shell commands is not guaranteed** — editors, permission modes, and agent configurations vary widely and may silently skip the command. If the daemon never starts, tasks appear to succeed but **no files are written to your workspace**. Installing and starting the daemon yourself before your first task eliminates this failure mode entirely.

---

### Step 2 — Register the MCP server

Once the daemon is running, add the Neo MCP server to your editor. There are two modes:

---

#### HTTP mode — hosted server, works in any editor, no local MCP install

```bash
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \  , 
  --header "Authorization: Bearer sk-v1-YOUR_KEY" 
```

The MCP server runs at `https://mcpserver.heyneo.com/mcp` — nothing to install or maintain locally. Your daemon (started in Step 1) handles all local execution. This is the recommended mode for Cursor, Windsurf, VS Code, Zed, Claude.ai, and ChatGPT.

---

#### stdio mode — local pip install, auto-starts daemon silently

```bash
pipx install neo-mcp
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp  
```

> `pipx` is recommended over `pip` — it installs CLI tools in isolated environments and works on all platforms including Ubuntu/Debian. Install it with `apt install pipx` or `pip install pipx`.


The MCP server runs as a local subprocess. In this mode the server auto-starts the daemon silently on first task submission — no manual daemon management needed. Recommended when you want a fully self-contained local setup.

---

Open a **new session** in your editor after registering. Neo tools appear automatically.

---

### Option: VS Code or Cursor extension (zero setup)

Install the [Neo extension](https://marketplace.visualstudio.com/items?itemName=NeoResearch.neo) from the marketplace and log in — the extension manages the daemon automatically, skipping Step 1 entirely. Then register the HTTP server above.

---

### Option: Claude Code `/neo` skill

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
