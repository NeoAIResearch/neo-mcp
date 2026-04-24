# Neo MCP — Usage Guide

---

## Prerequisites

- Neo account with a secret key (`sk-v1-...`) — from [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys
- Neo MCP connected to your editor — see [CLIENTS.md](CLIENTS.md) for setup instructions
- For local task execution: either the Neo VS Code/Cursor extension (zero setup), or the npm daemon (`npx --yes neo-mcp-daemon /workspace &`) — if no daemon is found your agent will offer to start one automatically

---

## How files work

Files are **always written to your local machine** — never to a remote sandbox.

The Neo daemon runs as a background process on your machine. It receives `write_code` commands from the Neo backend and writes files directly to your workspace. When the agent passes `workspace=/home/you/myproject`, that is exactly where the files appear.

Neo's backend uses `/app/project/` as its internal container path. When you see paths like `/app/project/src/model.py` in Neo's output messages, those map to `<workspace>/src/model.py` on your local filesystem — the daemon remaps them automatically. You do not need to do anything.

---

## How a task works

```
You type a prompt
      ↓
neo_submit_task   →  submits task  →  returns thread_id immediately
                     background polling starts automatically
      ↓
neo_task_status   →  check overall status at any time
      ↓
   COMPLETED            →  call neo_get_messages for full output
   WAITING_FOR_FEEDBACK →  call neo_send_feedback to reply, re-check status
   TERMINATED           →  task failed or was stopped
```

---

## Tools reference

| Tool | When to use |
|---|---|
| `neo_submit_task` | Start a task. Use `wait_for_completion=true` for short tasks (< 3 min) to get output immediately. |
| `neo_list_tasks` | Find running or recent tasks — useful after closing a window or losing track of a task. Reconnects pollers automatically. |
| `neo_task_status` | Quick overall status check: RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / PAUSED / TERMINATED. |
| `neo_get_messages` | Full conversation output once COMPLETED. Caps at ~20 000 tokens. |
| `neo_send_feedback` | Reply when Neo asks a question (WAITING_FOR_FEEDBACK). |
| `neo_pause_task` | Pause execution mid-task. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Cancel and clean up. |
| `neo_list_integrations` | Check which third-party keys are configured (GitHub / HuggingFace / Anthropic / OpenRouter). Returns names only — never the secret value. |
| `neo_add_integration` | Register a third-party API key locally. Example: *"save my OpenRouter key sk-or-... for Neo"*. |
| `neo_test_integration` | Verify a stored key still works against the provider's API. Run this first when a Neo task fails with a 401/403. |
| `neo_remove_integration` | Delete a stored key from this machine. Irreversible — re-add via `neo_add_integration` to use again. |

> **Integrations** store keys locally (`~/.neo/integrations/*.env` mode `0o600`, or OS keyring with `NEO_INTEGRATIONS_BACKEND=keyring`). Keys never leave your machine. Full guide: [INTEGRATIONS.md](INTEGRATIONS.md).

---

## Example prompts

**Train a model**
```
Use Neo to build a churn prediction model on churn.csv, optimise for recall
```

**Quick task — get result immediately**
```
Use Neo to create a sentiment analysis script for product reviews (wait for completion)
```
→ passes `wait_for_completion=true`, blocks until done, returns output directly

**Check live progress**
```
What is Neo working on right now?
```
→ calls `neo_task_status` — shows current overall status

**Data analysis**
```
Use Neo to analyse dataset.csv and suggest the best ML approach
```

**Feature engineering**
```
Use Neo to engineer features from transactions.csv for a fraud detection model
```

---

## Replying mid-task (WAITING_FOR_FEEDBACK)

When Neo needs clarification it pauses and waits. Reply naturally:

```
Tell Neo to use XGBoost and target the "churned" column
```

The assistant calls `neo_send_feedback` and Neo resumes automatically.

---

## Pausing and resuming

```
Pause the Neo task
Resume the Neo task
```

---

## Stopping a task

```
Stop the Neo task and clean up
```

---

## Output truncation

`neo_get_messages` caps at ~80 000 characters (~20 000 tokens). For a quick status check while the task is running, use `neo_task_status` instead — it reads from the in-memory cache without fetching the full message history.

---

## Workflow: recommended polling pattern

For tasks that take a few minutes:
1. `neo_submit_task` → get `thread_id`
2. `neo_task_status` → check status (repeat until COMPLETED)
3. `neo_get_messages` → read the final output

For short tasks (< 3 min):
1. `neo_submit_task` with `wait_for_completion=true` → output returned directly

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Invalid API key` (401) | Wrong key | Re-check `NEO_SECRET_KEY` |
| `Trial or quota ended` (403) | Out of credits | Top up at Neo dashboard |
| Task submitted but no files appear locally | Daemon failed to auto-start | Start npm daemon: `NEO_SECRET_KEY=sk-v1-... npx --yes neo-mcp-daemon /your/workspace &` or Python fallback: `neo-mcp daemon`. Files land in the `workspace` you passed to `neo_submit_task` (the agent picks this up from your project context automatically). |
| Status stuck on RUNNING | Step waiting for daemon response | Check `neo_task_status` to see the current status |
| `neo-mcp` not found | Install incomplete | Re-run `pip install neo-mcp`, check PATH |

---

## For maintainers — publishing

PyPI publish triggers automatically on version tags (`v*`). npm publish triggers automatically on `v*` tags.
