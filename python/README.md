# Neo MCP — AI engineering, without leaving your editor

<!-- mcp-name: io.github.NeoAIResearch/neo-mcp -->

[![PyPI](https://img.shields.io/pypi/v/neo-mcp.svg)](https://pypi.org/project/neo-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/neo-mcp.svg)](https://pypi.org/project/neo-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Downloads](https://static.pepy.tech/badge/neo-mcp/month)](https://pepy.tech/project/neo-mcp)

**Stay in Claude Code, Cursor, or Codex and hand your AI/ML work to Neo — an autonomous AI engineer that plans, builds, runs, and evaluates entire workflows from a plain-English request.** `neo-mcp` is the [Model Context Protocol](https://modelcontextprotocol.io) server that connects Neo to the AI coding tools you already use, so you never switch tabs: ask your agent to send a task to Neo, and a local daemon writes the resulting code, models, metrics, and reports straight into your repo — on your machine, nothing stored remotely.

Because Neo is purpose-optimized for AI engineering — not a general-purpose coding assistant — it goes **deeper on ML, LLM, and data workflows** than a general coding agent can.

📚 **Docs:** https://docs.heyneo.com/neo-mcp  ·  🔑 **Get an API key:** [Neo dashboard](https://heyneo.com/dashboard?section=settings#access-keys)

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

## Install

```bash
pip install neo-mcp
```

Requires Python 3.11+.

> **Tip:** use `pipx install neo-mcp` to install in an isolated environment and avoid conflicts with your project's virtualenv.

---

## Use Neo from your editor

Neo runs in every major MCP-enabled AI editor — set it up once below. Replace `sk-v1-YOUR_KEY` with your actual key.

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
| `neo_list_integrations` | List stored third-party API keys (names only — never the value). |
| `neo_add_integration` | Register a GitHub PAT / HuggingFace token / Anthropic key / OpenRouter key so Neo tasks can use it as an env var. |
| `neo_test_integration` | Call the provider's API to confirm a stored key is still valid. |
| `neo_remove_integration` | Delete a stored key from this machine. |

> **Integration tools** store credentials locally (file `0o600` under `~/.neo/integrations/`, or OS keyring with `NEO_INTEGRATIONS_BACKEND=keyring`). Keys never leave your machine. See the full guide at [docs/INTEGRATIONS.md](https://github.com/NeoAIResearch/neo-mcp/blob/main/docs/INTEGRATIONS.md).

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `NEO_SECRET_KEY` | **Yes** | Your Neo API key (`sk-v1-...`) from the [Neo dashboard](https://heyneo.com/dashboard?section=settings#access-keys) |
| `NEO_DEPLOYMENT_ID` | No | Pin a specific deployment UUID (auto-generated and persisted by default) |
| `NEO_WORKSPACE_DIR` | No | Override working directory (useful in Docker) |
| `NEO_READ_ONLY` | No | `true` — expose only status/message tools, disable submit/stop/pause |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `neo-mcp: command not found` | Re-run `pip install neo-mcp` and verify your PATH with `which neo-mcp` |
| Tools don't appear after registering | Open a **new session** — MCP tools load at session start, not mid-session |
| `Invalid API key` (401) | Re-check your key in the [Neo dashboard](https://heyneo.com/dashboard?section=settings#access-keys) |
| `Trial or quota ended` (403) | Top up at the Neo dashboard |
| Task submitted but no files written | Daemon failed to start — run `neo-mcp doctor` to diagnose |
| Status stuck on RUNNING | Call `neo_task_status` to check; run `neo-mcp status` to inspect the daemon |
