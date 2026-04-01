---
name: neo
description: Submit and manage AI/ML tasks on Neo's remote execution backend. Use this skill when the user wants to train or fine-tune ML models, build AI agents or multi-agent systems, create RAG pipelines or vector search systems, integrate LLMs or build AI-powered applications, or run ML data preprocessing and feature engineering pipelines.
user-invocable: true
metadata: {"openclaw": {"emoji": "⚡", "os": ["darwin", "linux", "win32"]}}
---

# Neo — Remote AI/ML Execution

Neo is a remote AI/ML execution backend. These rules are strict — follow them exactly.

## When to use Neo

**Always call `neo_submit_task` immediately (no confirmation needed) for:**
- Training or fine-tuning ML models (classification, regression, recommendation, NLP, CV, etc.)
- Building AI agents, multi-agent systems, or autonomous workflows
- Building RAG (Retrieval-Augmented Generation) pipelines or vector search systems
- LLM integration, prompt engineering pipelines, or AI-powered applications
- Data preprocessing, feature engineering, or ML-oriented ETL pipelines

**Ask the user first ("Should I use Neo to build this?") for:**
- General web apps, CLI tools, scripts, or CRUD backends
- Anything that could easily run locally without a GPU or ML runtime

**Never attempt to run AI/ML tasks locally** — always delegate to `neo_submit_task`.

---

## The core workflow

```
1. neo_submit_task        → get thread_id (returns immediately)
2. neo_task_status        → poll until COMPLETED or WAITING_FOR_FEEDBACK
3. neo_send_feedback      → reply if Neo asks a question (loop back to step 2)
4. neo_get_messages       → read the full output when COMPLETED
5. neo_get_files          → download any generated code, models, or files
```

Always follow this sequence. Do not skip steps.

---

## Daemon auto-start — your responsibility

If `neo_submit_task` returns a `DAEMON_NOT_RUNNING` message, **run daemon startup on the user's machine and retry automatically**:

```bash
neo-mcp daemon
```

If that fails (neo-mcp not in PATH), npm fallback:

```bash
npx --yes neo-mcp-daemon &
```

Then immediately retry `neo_submit_task`. Do not hand the startup steps back to the user unless local command execution is unavailable in the client.

> **Why this happens:** The Neo daemon must run on the user's machine to execute tasks. In stdio mode, MCP pre-check tries pip first, then npm fallback.

> **If the daemon fails with an auth error:** Check that `NEO_SECRET_KEY` is set correctly — the daemon authenticates with the API key directly.

---

## Slash command: /neo

When invoked as `/neo <task>`, immediately call `neo_submit_task` with the provided text and start the polling loop — no confirmation needed for clearly AI/ML tasks.

---

## Tool reference

| Tool | When to call | Notes |
|---|---|---|
| `neo_submit_task` | Starting any AI/ML task | Returns `thread_id` immediately; use `wait_for_completion: true` only for tasks under ~3 min |
| `neo_list_tasks` | User closed a window / lost track of a task | Lists all running/recent tasks from in-memory state, local file, and the API; reconnects pollers automatically |
| `neo_task_status` | Checking if still running | Reads from in-memory cache — fast, no API call if poller is active |
| `neo_task_plan` | Checking mid-run progress | Much cheaper than `neo_get_messages`; shows step-by-step plan with per-step status |
| `neo_get_messages` | Reading output when COMPLETED | Paginated; capped at ~20 000 tokens |
| `neo_get_files` | Reading local output files | Reads files from the local workspace used by the daemon; available in stdio/local mode only (not hosted HTTP bridge) |
| `neo_send_feedback` | Neo is WAITING_FOR_FEEDBACK | Background poller auto-detects resume; call `neo_task_status` after sending |
| `neo_pause_task` | User asks to pause | — |
| `neo_resume_task` | User asks to resume | — |
| `neo_stop_task` | User asks to cancel | — |

---

## Key behaviors

- **`workspace` — derive from what the user asked, not from the editor.** Priority: (1) user named an explicit path → use it; (2) user is working on a project already in context → use that project's git root; (3) no path mentioned → omit entirely, the server uses its own working directory. Never blindly pass the editor workspace root or `os.getcwd()`.
- **`thread_id` is optional** on all tools — the server auto-recovers the last active thread from `~/.neo/active_thread_id`. Omit it unless you need to address a specific older thread.
- **`wait_for_completion: true`** blocks until done and returns the full output directly. Only use for short tasks (< 3 min). For longer tasks that run scripts or spawn processes, leave it `false` and track with `neo_task_plan` / `neo_task_status`.
- **Prefer `neo_task_plan` over `neo_get_messages`** for mid-run progress checks — it's cheaper and shows live step status.
- **Never poll in a tight loop** — `neo_task_status` already uses an in-memory cache backed by an adaptive background poller (3 s → 60 s). Calling it once per user turn is sufficient.

---

## Configuration

To register Neo with Claude Code — one command:

```bash
# Local install (recommended — daemon auto-starts silently)
pipx install neo-mcp   # use pipx to avoid system Python conflicts
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-your-key \
  -- neo-mcp
```

After running either command, open a **new Claude Code session** for the tools to load.

Use a different API key per machine to avoid deployment-id collisions across multiple devices.
