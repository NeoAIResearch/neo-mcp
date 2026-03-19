# Neo MCP Server

Run AI/ML tasks on Neo's remote backend directly from Claude Code, Cursor, Windsurf, Zed, VS Code, Continue.dev, and OpenAI Codex CLI.

---

## Quickstart

```bash
pip install neo-mcp
neo-mcp setup
```

The setup wizard detects your editors, prompts for your Neo API keys, and writes all config files automatically.

---

## No-install option — hosted endpoint

Point any MCP client directly at the hosted server:

| Field | Value |
|---|---|
| URL | `https://mcp.heyneo.so/mcp` |
| Header | `x-access-key: ak-v1-...` |
| Header | `Authorization: Bearer sk-v1-...` |

Get your keys from the **Neo dashboard**.

> **Note:** The hosted endpoint routes tasks to Neo's cloud backend. For local file access and subprocess execution, use the local `pip install` mode (stdio) so the built-in daemon runs on your machine.

---

## What you need

- Neo account with API keys (`ak-v1-...` and `sk-v1-...`)
- Python 3.11+ **or** Docker

No VS Code extension required.

---

## What Neo is for

Neo runs AI/ML work on a remote backend so it does not block your local machine:

- Training or fine-tuning models (classification, regression, NLP, computer vision)
- Building AI agents and multi-agent workflows
- RAG pipelines and vector search systems
- LLM integrations and prompt engineering pipelines
- ML data preprocessing and feature engineering

For general coding your assistant works locally — it only routes to Neo for AI/ML tasks.

---

## Example session

```
You:       "Train a fraud detection model on fraud.csv"
Assistant: Submitting to Neo…
           Neo is working — preprocessing data, training XGBoost…
           Done. AUC-ROC: 0.94. Model saved to fraud_model.pkl
```

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

## Manual registration (Claude Code)

```bash
# Local — pip (recommended for full functionality)
claude mcp add --scope user neo \
  -e NEO_API_KEY=ak-v1-... -e NEO_SECRET_KEY=sk-v1-... \
  -- neo-mcp

# Remote — hosted endpoint (no local install)
claude mcp add --transport http neo https://mcp.heyneo.so/mcp \
  --header "x-access-key: ak-v1-..." \
  --header "Authorization: Bearer sk-v1-..."
```

See [docs/CLIENTS.md](docs/CLIENTS.md) for all editors.

---

## Configuration

| Variable | Required | Description |
|---|---|---|
| `NEO_API_KEY` | Yes | Access key (`ak-v1-...`) |
| `NEO_SECRET_KEY` | Yes | Secret key (`sk-v1-...`) |
| `NEO_READ_ONLY` | No | `true` = expose only status/message tools |
| `NEO_WORKSPACE_DIR` | No | Override working directory (useful in Docker) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Invalid API key` (401) | Re-check `NEO_API_KEY` and `NEO_SECRET_KEY` |
| `Trial or quota ended` (403) | Top up at Neo dashboard |
| Task submitted but no files written locally | Use local stdio mode (`pip install neo-mcp`), not the hosted endpoint |
| `neo-mcp` command not found | Re-run `pip install neo-mcp` and check your PATH |
| Output truncated | Cap is ~20 000 tokens — ask for earlier messages to page back |
