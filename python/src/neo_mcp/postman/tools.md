# Neo MCP Tools

| Tool | Description |
|------|-------------|
| `neo_submit_task` | Submit a locally destructive task. Enforces one active owner per workspace. |
| `neo_list_tasks` | List all running and recent tasks. |
| `neo_task_status` | Compact live backend telemetry; status is reported, not independently verified. |
| `neo_get_messages` | Read full task output when COMPLETED. |
| `neo_send_feedback` | Reply when Neo asks a clarifying question (WAITING_FOR_FEEDBACK). |
| `neo_pause_task` | Pause a running task. |
| `neo_resume_task` | Resume a paused task. |
| `neo_stop_task` | Permanently stop and clean up a task. |
| `neo_list_integrations` | List stored third-party API keys (names only). |
| `neo_add_integration` | Register a credential for Neo task subprocesses. |
| `neo_test_integration` | Verify a stored key against the provider API. |
| `neo_remove_integration` | Delete a stored key from this machine. |
| `neo_get_execution_evidence` | Read bounded local command/file hash observations. |
| `neo_verify_task` | Run explicit file assertions and optional bounded acceptance commands. |

## Key parameters

**neo_submit_task**

- `message` — full task description (goal, paths, constraints)
- `workspace` — absolute path to project/git root (never a subdirectory)
- `submission_id` — optional UUID; reuse only after an ambiguous timeout

**neo_task_status / neo_get_messages / neo_send_feedback**

- `thread_id` — from `neo_submit_task` response

Messages and activity are telemetry, not proof. Only `neo_verify_task` can
produce `VERIFIED`; its evidence is local observation rather than remote
attestation.
