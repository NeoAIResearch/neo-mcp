# Neo MCP Tools

| Tool | Description |
|------|-------------|
| `neo_submit_task` | Submit a task to Neo. Returns `thread_id` immediately. |
| `neo_list_tasks` | List all running and recent tasks. |
| `neo_task_status` | Check status: RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / PAUSED / TERMINATED. |
| `neo_get_messages` | Read full task output when COMPLETED. |
| `neo_send_feedback` | Reply when Neo asks a clarifying question (WAITING_FOR_FEEDBACK). |
| `neo_pause_task` | Pause a running task. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Permanently stop and clean up a task. |
| `neo_list_integrations` | List stored third-party API keys (names only). |
| `neo_add_integration` | Register a credential for Neo task subprocesses. |
| `neo_test_integration` | Verify a stored key against the provider API. |
| `neo_remove_integration` | Delete a stored key from this machine. |

## Key parameters

**neo_submit_task**

- `message` — full task description (goal, paths, constraints)
- `workspace` — absolute path to project/git root (never a subdirectory)

**neo_task_status / neo_get_messages / neo_send_feedback**

- `thread_id` — from `neo_submit_task` response
