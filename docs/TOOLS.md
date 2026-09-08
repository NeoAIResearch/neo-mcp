# Neo MCP — Tool Reference

Complete list of tools exposed by the Neo MCP server (Python `neo_mcp.server` / npm `mcp-server.ts`).

**Total: 17 tools** — 8 task lifecycle · 4 integrations · 5 BYOK

Source of truth: [`python/src/neo_mcp/server.py`](../python/src/neo_mcp/server.py)

> **Note:** [`mcpb/manifest.json`](../mcpb/manifest.json) lists only 12 tools and is out of date — it omits the five BYOK tools below.

---

## Typical workflow

```
neo_submit_task
    → neo_task_status (poll)
        ├─ COMPLETED                          → neo_get_messages
        ├─ WAITING_FOR_FEEDBACK, new instructions → neo_send_feedback (resumes + delivers message)
        ├─ WAITING_FOR_FEEDBACK, nothing new      → neo_resume_task (resumes unchanged)
        └─ (user cancels)                     → neo_stop_task
```

> `neo_pause_task` causes `neo_task_status` to report `WAITING_FOR_FEEDBACK` — there is no separate status to check for a paused task.

Use `neo_list_tasks` when returning to a session to find active or recent `thread_id`s.

---

## Task lifecycle (8 tools)

### `neo_submit_task`

Submit an AI/ML task to Neo for local execution.

**Use for:** training/fine-tuning models, building AI agents, RAG pipelines, LLM integrations, ML data processing. **Not** for general coding — write that directly in the editor.

Execution is local: the daemon writes files to `workspace`. Neo may reference `/app/project/...` in output; the daemon remaps that to `<workspace>/...`.

**Returns:** `{ thread_id, status, workspace }` immediately. Then poll with `neo_task_status`; read output with `neo_get_messages` when `COMPLETED`.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `message` | yes | Full task description — goal, paths, constraints. If the user named a model or external ID, include the **exact string** they used (never a substitute). |
| `workspace` | yes | Absolute **project root** (git root). Never a subdirectory. Infer via `git rev-parse --show-toplevel` or `os.getcwd()`. |
| `wrapper_hint` | no | Optional Neo-generated project slug for wrapper-stripping (e.g. `ml_project_0855`). Only use when you already know Neo's container slug — not user folder names like `demo` or `src`. |

**Model and ID fidelity:** If the user names a model, API, package, dataset, or other discrete ID, copy it verbatim into `message`. Do not substitute, upgrade, downgrade, or normalize (e.g. do not replace `gemini 3.1 pro` with `gpt-4o`). Do not call `neo_list_byok_models` or guess IDs when the user already specified one.

---

### `neo_task_status`

Get the current status of a Neo task.

**Returns one of:**

| Status | Meaning | Next step |
|--------|---------|-----------|
| `RUNNING` | Still executing | Call again (once per turn) |
| `COMPLETED` | Done | `neo_get_messages` |
| `WAITING_FOR_FEEDBACK` | Neo has a question, or is frozen after `neo_pause_task` | `neo_send_feedback` if you have new instructions (resumes + delivers), otherwise `neo_resume_task` (resumes unchanged) |
| `TERMINATED` / `FAILED` | Ended | `neo_get_messages` to read what happened |

Reads from an in-memory cache (adaptive poller 3s–60s). Do not poll in a tight loop.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `thread_id` | yes | From `neo_submit_task` |

---

### `neo_get_messages`

Retrieve full conversation output from a **completed** task. Paginated (~80k chars cap).

Only call when `neo_task_status` returns `COMPLETED`. For live progress while `RUNNING`, prefer lighter status checks.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `thread_id` | yes | Task thread ID |
| `before` | no | ISO timestamp cursor for pagination (oldest message from previous page) |
| `limit` | no | Max messages per page (default 50, max 200) |

---

### `neo_send_feedback`

Reply when Neo is `WAITING_FOR_FEEDBACK` — this single call resumes the task and delivers your message. This is also the status reported after `neo_pause_task`. Do **not** use to start a new task — use `neo_submit_task`.

To course-correct a `RUNNING` task, call `neo_pause_task` first, then call `neo_send_feedback` right away once `neo_task_status` reports `WAITING_FOR_FEEDBACK`.

If the task is `WAITING_FOR_FEEDBACK` and you have nothing new to say, use `neo_resume_task` instead — `neo_send_feedback` requires a `message`.

After sending, call `neo_task_status` to confirm the task resumed.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `thread_id` | yes | Task that is `WAITING_FOR_FEEDBACK` |
| `message` | yes | Your reply or clarification. If clarifying a model or external ID, use the user's **exact wording** — never substitute a different model. |

**Model and ID fidelity:** When correcting which model or ID to use, pass the user's exact wording in `message`. Do not override with a default or "better" alternative.

---

### `neo_pause_task`

Pause a running task mid-execution. `neo_task_status` will then report `WAITING_FOR_FEEDBACK`. Safe to call on an already-paused task (no-op). Afterward: `neo_send_feedback` if you have new instructions (resumes + delivers), or `neo_resume_task` to continue unchanged.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `thread_id` | yes | Running task to pause |

---

### `neo_resume_task`

Resume a paused task exactly as before, with **no** new input. Works after `neo_task_status` reports `WAITING_FOR_FEEDBACK` following the pause. No effect if already running. Only works after `neo_pause_task`.

If you have instructions, a correction, or clarification to give, use `neo_send_feedback` instead — it resumes the task **and** delivers your message in one call. Calling `neo_resume_task` resumes silently; anything you wanted to say is lost.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `thread_id` | yes | Paused task to resume |

---

### `neo_stop_task`

Permanently stop and clean up a task. **Irreversible** — context is deleted; cannot resume. Use `neo_pause_task` for temporary freeze.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `thread_id` | yes | Task to stop |

---

### `neo_list_tasks`

List all known tasks with live status (newest first). Use after reopening the editor to find `RUNNING` / `COMPLETED` / waiting tasks.

**Returns per task:** `thread_id`, `workspace`, `status`, last-updated timestamp.

| Parameter | Required | Description |
|-----------|----------|-------------|
| *(none)* | — | |

---

## Integrations — task subprocess credentials (4 tools)

These give **Neo task subprocesses** API keys as environment variables (`ANTHROPIC_API_KEY`, `HF_TOKEN`, `GITHUB_TOKEN`, `OPENROUTER_API_KEY`, etc.). They do **not** change which LLM powers Neo's orchestrator — see BYOK below.

**Supported providers:** `github`, `huggingface`, `anthropic`, `openrouter`

---

### `neo_list_integrations`

List configured integrations (provider, auth method, when added). **Never returns secret values.**

| Parameter | Required | Description |
|-----------|----------|-------------|
| *(none)* | — | |

---

### `neo_add_integration`

Register a credential for Neo tasks. The supported way to save keys — do not tell users to create `.env` files instead.

**Key-prefix hints:** `sk-or-...` → openrouter · `sk-ant-...` → anthropic · `hf_...` → huggingface · `ghp_...` / `github_pat_...` → github

Relay the response `safety` message verbatim after success; never echo the key.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `provider` | yes | `github` \| `huggingface` \| `anthropic` \| `openrouter` |
| `credentials` | yes | Provider-specific fields, e.g. `{ "api_key": "..." }`, `{ "pat": "..." }`, `{ "token": "..." }` |

---

### `neo_remove_integration`

Delete a stored integration. Irreversible.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `provider` | yes | Provider to remove |

---

### `neo_test_integration`

Test a stored key against the provider API (read-only).

| Parameter | Required | Description |
|-----------|----------|-------------|
| `provider` | yes | Provider to test |

---

## BYOK — orchestrator LLM (5 tools)

**Bring your own key** for Neo's **orchestrator** (the agent brain that plans work). Sends `x-llm-key`, `x-llm-provider`, and `x-llm-model` headers on `neo_submit_task` and `neo_send_feedback` only.

Different from integrations: BYOK changes Neo's brain; integrations give task subprocesses env vars.

**Supported providers:** `anthropic`, `openai`, `openrouter`

---

### `neo_list_byok_profiles`

List BYOK profiles and which is active. Returns id, name, provider, model, masked key hint. Never returns raw keys.

| Parameter | Required | Description |
|-----------|----------|-------------|
| *(none)* | — | |

---

### `neo_add_byok_profile`

Create and validate a BYOK profile. Invalid keys are rejected. Active by default.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | yes | Friendly profile name |
| `provider` | yes | `anthropic` \| `openai` \| `openrouter` |
| `model` | yes | Exact model ID for that provider — pass user's discrete ID verbatim; do not substitute example models when the user named something else |
| `api_key` | yes | Provider API key (stored locally only) |
| `set_active` | no | Make active immediately (default `true`) |

---

### `neo_set_byok_profile`

Activate a profile or clear BYOK (`profile_id: null` → Neo uses its default credentials again).

| Parameter | Required | Description |
|-----------|----------|-------------|
| `profile_id` | yes | Profile id from `neo_list_byok_profiles`, or `null` to clear |

---

### `neo_remove_byok_profile`

Delete a profile and its key. If it was active, BYOK is cleared.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `profile_id` | yes | Profile id to delete |

---

### `neo_list_byok_models`

Discover model IDs a BYOK provider supports. Prefers Neo backend catalog; falls back to provider API or curated list. Response `source` indicates which was used.

Discovery only when the user **did not** name a model — never pick the first list entry over an explicit user ID.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `provider` | yes | `anthropic` \| `openai` \| `openrouter` |
| `api_key` | no | Optional — fetch live account model list |

---

## Quick reference table

| Tool | Category | Read-only |
|------|----------|-----------|
| `neo_submit_task` | Task | no |
| `neo_task_status` | Task | yes |
| `neo_get_messages` | Task | yes |
| `neo_send_feedback` | Task | no |
| `neo_pause_task` | Task | no |
| `neo_resume_task` | Task | no |
| `neo_stop_task` | Task | no |
| `neo_list_tasks` | Task | yes |
| `neo_list_integrations` | Integration | yes |
| `neo_add_integration` | Integration | no |
| `neo_remove_integration` | Integration | no |
| `neo_test_integration` | Integration | yes |
| `neo_list_byok_profiles` | BYOK | yes |
| `neo_add_byok_profile` | BYOK | no |
| `neo_set_byok_profile` | BYOK | no |
| `neo_remove_byok_profile` | BYOK | no |
| `neo_list_byok_models` | BYOK | yes |

---

## Related docs

- [USAGE.md](USAGE.md) — day-to-day workflow
- [INTEGRATIONS.md](INTEGRATIONS.md) — integrations vs BYOK in depth
- [CLIENTS.md](CLIENTS.md) — Claude Code, Cursor, Codex setup
- [skills/claude-code/SKILL.md](../skills/claude-code/SKILL.md) — `/neo` slash command behavior
