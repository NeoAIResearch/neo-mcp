# Neo MCP — Your autonomous AI Engineering agent

<!-- mcp-name: io.github.NeoAIResearch/neo-mcp -->

[![PyPI](https://img.shields.io/pypi/v/neo-mcp.svg)](https://pypi.org/project/neo-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/neo-mcp.svg)](https://pypi.org/project/neo-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Downloads](https://static.pepy.tech/badge/neo-mcp/month)](https://pepy.tech/project/neo-mcp)

**[neo-mcp](https://docs.heyneo.com/neo-mcp) is the [Model Context Protocol](https://modelcontextprotocol.io) server that connects Neo — an autonomous AI engineer — to Claude Code, Cursor, Codex, and the editors you already use. Describe any AI/ML task in plain English; Neo plans, builds, runs, and evaluates the full workflow.**

**A local daemon writes every artifact straight into your repo on your machine — code, models, metrics, and reports. Nothing is stored remotely.**

Because Neo is purpose-optimized for AI engineering — not a general-purpose coding assistant — it goes **deeper on ML, LLM, and data workflows** than a general coding agent can.

🌐 **[Neo](https://heyneo.com)**  ·  📚 **[Docs](https://docs.heyneo.com/neo-mcp)**  ·  🔑 **Get an API key:** [Neo dashboard](https://heyneo.com/dashboard?section=settings#access-keys)

## See it in action

[![Neo MCP demo — Codex + Neo in action](artifacts/neo-mcp-demo.gif)](https://heyneo-content.s3.us-east-2.amazonaws.com/documents/public/codex-neo-mcp-demo.mp4)

*Click to watch the full demo with sound.*

## What MCP unlocks

- 🧩 **Stay in your editor** — drive Neo from Claude Code, Cursor, VS Code (Copilot), Windsurf, Zed, Continue, or Codex. No new app, no context switching.
- 🔬 **More depth** — autonomous planning, experiments, evaluation, and iteration tuned for AI/ML, beyond what a generic coding agent attempts.
- 💾 **Local-first** — every output file is written to your machine; nothing is stored remotely.

## What you can build with Neo

- 🤖 **Generative AI & LLMs** — RAG & semantic search, agents & chatbots, fine-tuning (Llama, Qwen, Gemma), document analysis
- 🧠 **ML & deep learning** — PyTorch / TensorFlow / scikit-learn training, architecture search, evaluation
- 📊 **Data science & analytics** — EDA, feature engineering, forecasting, segmentation, A/B testing, reporting
- 👁️ **Computer vision** — image classification, object detection, OCR
- 🎤 **Speech & audio** — speech-to-text, text-to-speech, audio classification
- 🔌 **Bring your own keys** — GitHub, HuggingFace, Anthropic, OpenRouter, OpenAI, AWS S3, Weights & Biases, Kaggle — stored locally, injected as env vars

> For data scientists, ML & LLM engineers, analysts, researchers, and PMs who want results, not boilerplate.

---

## Try it

Ask your agent to use Neo — for example:

```
Use Neo to fix the failing training run and re-run with logging
```

```
Benchmark these prompts on our eval set using Neo
```

```
Build or debug an end-to-end ML pipeline using Neo
```

```
Train a fraud detection model on fraud.csv, optimize for recall
```

```
Fine-tune a text classifier on my training data with 5-fold cross-validation
```

Neo handles the ML execution — your editor handles everything else.

---

## Install

```bash
pip install neo-mcp
```

Requires Python 3.11+.

> **Tip:** use `pipx install neo-mcp` to install in an isolated environment and avoid conflicts with your project's virtualenv.

---

## Use Neo from your editor

Replace `sk-v1-YOUR_KEY` with your actual API key.

After setup, ask your agent: *"What Neo tools do you have available?"* — it should list `neo_submit_task`, `neo_task_status`, `neo_get_messages`, and more.

---

### Claude Code

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
```

Open a **new Claude Code session** after running this. Neo tools load at session start, not mid-session.

> **Scope options:** `--scope user` (global, recommended) · `--scope project` (writes `.mcp.json` in current repo) · `--scope local` (this machine only)

Verify it registered:
```bash
claude mcp list
```

You should see `neo` with a green checkmark.

---

### Cursor

**Open the config:**
- GUI: `Ctrl+Shift+J` (Windows/Linux) or `Cmd+Shift+J` (Mac) → **Tools & MCP** → **New MCP Server**
- Or edit the file directly: `~/.cursor/mcp.json`

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

Restart Cursor after editing the file directly. Changes via the GUI apply immediately.

---

### OpenAI Codex CLI

**Open the config:**
- Run `codex mcp` to manage servers interactively via CLI
- Or edit the file directly: `~/.codex/config.json`

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

---

### Also works with

**Windsurf** — `~/.codeium/windsurf/mcp_config.json`:

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

**VS Code (GitHub Copilot)** — `.vscode/mcp.json` in your workspace root (requires VS Code 1.99+, Agent mode):

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

**Zed** — `~/.config/zed/settings.json`:

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

**Continue.dev** — `~/.continue/config.yaml`:

```yaml
mcpServers:
  - name: neo
    command: neo-mcp
    env:
      NEO_SECRET_KEY: sk-v1-YOUR_KEY
```

> GUI paths and per-editor notes: [docs/GUIDE.md](docs/GUIDE.md)

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

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `NEO_SECRET_KEY` | **Yes** | API key (`sk-v1-...`) from [heyneo.com/dashboard](https://heyneo.com/dashboard?section=settings#access-keys) → Settings → API Keys |
| `NEO_DEPLOYMENT_ID` | No | Pin a specific daemon UUID (auto-generated by default) |
| `NEO_WORKSPACE_DIR` | No | Override workspace directory (useful in Docker or CI) |
| `NEO_READ_ONLY` | No | `true` = expose only status/message tools — disables submit, stop, and pause |

---

## Diagnostics

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

## Troubleshooting

| Symptom | Fix |
|---|---|
| `neo-mcp: command not found` | Re-run `pip install neo-mcp`, verify with `which neo-mcp` |
| `✗ Failed to connect` in `claude mcp list` | Run `claude mcp logs neo` — most common cause is `NEO_SECRET_KEY` not set |
| Neo tools don't appear | Open a **new session** — tools load at session start, not mid-session |
| `Invalid API key` (401) | Re-check your key at [heyneo.com/dashboard](https://heyneo.com/dashboard?section=settings#access-keys) → Settings → API Keys |
| `Trial or quota ended` (403) | Top up at the Neo dashboard |
| `No healthy deployments available` (400) | Daemon failed to auto-start — restart the MCP server and try again |
| Task submitted but no files written | Daemon stopped mid-task — check `neo-mcp status` and restart |
| Status stuck on `RUNNING` | Run `neo-mcp doctor` to diagnose; restart the MCP server |
| Output truncated | ~20 000 token cap — use `neo_task_status` for progress, `neo_get_messages` for final output only |

---

Full setup guide (all editors, GUI paths): [docs/GUIDE.md](docs/GUIDE.md) · [Docs](https://docs.heyneo.com/neo-mcp)
