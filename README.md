# Neo MCP Server

Run AI/ML tasks on Neo's remote backend directly from Claude Code, Cursor, Windsurf, Zed, VS Code, Continue.dev, and OpenAI Codex CLI.

---

## Quickstart — zero to working in under 5 minutes

```bash
pip install neo-mcp
neo-mcp setup
```

The setup wizard detects your editors, prompts for your Neo API keys, and writes all config files automatically. Restart your editor and the 7 Neo tools will be available.

---

## Quickstart (no install) — hosted endpoint

For Cursor, Windsurf, and VS Code you can skip `pip install` entirely and point directly at the hosted server:

| Field | Value |
|---|---|
| URL | `https://mcp.heyneo.so/mcp` |
| Header | `x-access-key: ak-v1-...` |
| Header | `Authorization: Bearer sk-v1-...` |

Get your keys from the **Neo dashboard**.

---

## What you need

- Neo account with API keys (`ak-v1-...` and `sk-v1-...`)
- Neo VS Code extension installed and connected (required for task execution)
- Python 3.11+ **or** Docker

---

## How to use Neo once it's connected

You don't call the tools directly — you just talk to your AI assistant in plain language. It decides when to route work to Neo and handles all the tool calls automatically.

### What Neo is for

Neo runs AI/ML work on a remote backend so it doesn't block your local machine:

- Training or fine-tuning models (classification, regression, NLP, computer vision, …)
- Building AI agents or multi-agent workflows
- RAG pipelines and vector search systems
- LLM integrations and prompt engineering pipelines
- ML data preprocessing and feature engineering

For general coding (web apps, scripts, CRUD backends) your assistant will work locally as normal — it only routes to Neo for AI/ML tasks.

---

### What a typical session looks like

```
You:       "Use Neo to train a fraud detection model on fraud.csv"

Assistant: Submitting your task to Neo now...
           [calls neo_submit_task — returns thread_id, starts background polling]

Neo asks:  "I see fraud.csv has 50k rows. Should I handle class imbalance
           with SMOTE or class weights?"

You:       "Use class weights"

Assistant: Sending your reply to Neo...
           [calls neo_send_feedback — Neo resumes automatically]

You:       "Is it done yet?"

Assistant: Status: COMPLETED. Reading the output...
           [calls neo_get_messages]

           Neo trained a Random Forest + XGBoost ensemble.
           Recall: 0.91 on the test set.
           Model saved to ./models/fraud_model.pkl
```

---

### Example prompts to try

**Submit a task**
> "Use Neo to build a churn prediction model on my `churn.csv`, optimise for recall"

> "Ask Neo to build a RAG pipeline for the PDF documents in my `/docs` folder"

> "Use Neo to fine-tune a sentiment classifier on `train.jsonl`"

**Check progress**
> "What's the status of the Neo task?"

> "Is Neo done yet?"

**Reply to a question**

Neo sometimes pauses and asks a clarifying question. Your assistant shows what Neo asked. Reply naturally:

> "Tell Neo to use XGBoost and target the `churned` column"

**Read the output**
> "Show me what Neo built"

> "Get the results from the Neo task"

**Control a running task**
> "Pause the Neo task"

> "Stop the Neo task and clean up"

---

## Available tools

| Tool | Description |
|---|---|
| `neo_submit_task` | Submit an AI/ML task. Returns a thread_id; background polling tracks progress. |
| `neo_task_status` | Check task status: RUNNING, WAITING_FOR_FEEDBACK, PAUSED, COMPLETED, TERMINATED. |
| `neo_get_messages` | Read the full output once the task is COMPLETED. |
| `neo_send_feedback` | Reply to Neo when it asks a question (WAITING_FOR_FEEDBACK). |
| `neo_pause_task` | Pause a running task. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Stop and clean up a task. |

---

## Manual install options

### pip (PyPI)
```bash
pip install neo-mcp
```

### Docker
```bash
docker pull ghcr.io/heyneo/neo-mcp-server
```

### pip from GitHub
```bash
pip install git+https://github.com/NeoResearchAI/MCPServer.git#subdirectory=neo-mcp
```

---

## Manual Claude Code registration

If you prefer not to use the wizard:

```bash
# Local (stdio)
claude mcp add --scope user neo \
  -e NEO_API_KEY=ak-v1-... -e NEO_SECRET_KEY=sk-v1-... \
  -- neo-mcp

# Remote (no local install required)
claude mcp add --transport http neo https://mcp.heyneo.so/mcp \
  --header "x-access-key: ak-v1-..." \
  --header "Authorization: Bearer sk-v1-..."
```

See [docs/SETUP.md](docs/SETUP.md) for all clients and options.

---

## Configuration scopes (Claude Code)

| Scope | Flag | Saved to | When to use |
|---|---|---|---|
| `user` | `--scope user` | `~/.claude/settings.json` | Personal use across all projects |
| `project` | `--scope project` | `.mcp.json` in repo | Team-shared setup |
| `local` | *(default)* | `.claude/settings.local.json` | One project, not committed |

---

## Read-only mode

Set `NEO_READ_ONLY=true` to expose only `neo_task_status` and `neo_get_messages`.

```bash
docker run -i --rm -e NEO_API_KEY -e NEO_SECRET_KEY -e NEO_READ_ONLY=true \
  ghcr.io/heyneo/neo-mcp-server
```
