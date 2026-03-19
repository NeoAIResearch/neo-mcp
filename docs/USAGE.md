# Neo MCP — Usage Guide

---

## Prerequisites

- Neo account with API keys — from the **Neo dashboard**
- MCP server registered in your editor (see `docs/CLIENTS.md`)
- `NEO_API_KEY` and `NEO_SECRET_KEY` set in the MCP config

---

## How a task works

```
You type a prompt
      ↓
neo_submit_task   → submits task → returns thread_id immediately
                    background polling starts automatically
      ↓
neo_task_status   → check progress at any time
      ↓
   COMPLETED           → call neo_get_messages for full output
   WAITING_FOR_FEEDBACK → call neo_send_feedback to reply, then re-check status
   TERMINATED          → task failed or was stopped
```

---

## Example prompts

**Train a model**
```
Use Neo to build a churn prediction model on churn.csv, optimise for recall
```

**Data analysis**
```
Use Neo to analyse dataset.csv and suggest the best ML approach
```

**Feature engineering**
```
Use Neo to engineer features from transactions.csv for a fraud detection model
```

**Check a running task**
```
Check the status of my Neo task
```

**Read completed output**
```
Show me what Neo built
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

## Available tools

| Tool | Description |
|---|---|
| `neo_submit_task` | Submit an AI/ML task. Starts background polling. |
| `neo_task_status` | Check current status. |
| `neo_get_messages` | Read output (caps at ~20 000 tokens). |
| `neo_send_feedback` | Send a reply when Neo is waiting. |
| `neo_pause_task` | Pause a running task. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Cancel and clean up. |

---

## Output truncation

`neo_get_messages` caps at ~80 000 characters (~20 000 tokens). If the output is cut off, ask for earlier messages to page back through the full output.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Invalid API key` (401) | Wrong keys | Re-check `NEO_API_KEY` and `NEO_SECRET_KEY` |
| `Trial or quota ended` (403) | Out of credits | Top up at Neo dashboard |
| Task submitted but no files appear locally | Using hosted endpoint (remote daemon) | Switch to local `pip install neo-mcp` (stdio mode) |
| Task appears stuck | Long-running task | Use `neo_task_status` + `neo_get_messages` manually |
| `neo-mcp` not found | Install incomplete | Re-run `pip install neo-mcp`, check PATH |

---

## For maintainers — publishing

The GitHub Actions workflow at `.github/workflows/publish-mcp.yml` builds and pushes to `ghcr.io/heyneo/neo-mcp-server` automatically on every push to `main` that touches `src/`, `Dockerfile`, `pyproject.toml`, or `requirements.txt`.

**Manual push:**
```bash
docker build -t ghcr.io/heyneo/neo-mcp-server:latest .
echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_USERNAME --password-stdin
docker push ghcr.io/heyneo/neo-mcp-server:latest
```

**Verify the image:**
```bash
docker run -i --rm \
  -e NEO_API_KEY=your-key \
  -e NEO_SECRET_KEY=your-secret \
  ghcr.io/heyneo/neo-mcp-server:latest
```

PyPI publish triggers automatically on version tags (`v*`).
