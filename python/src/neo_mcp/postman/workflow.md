# Neo MCP Workflow

## Standard flow

```
neo_submit_task  →  returns thread_id
      ↓
neo_task_status  →  poll until COMPLETED or WAITING_FOR_FEEDBACK
      ↓
neo_get_messages →  read full output
```

## Mid-task question

When `neo_task_status` returns `WAITING_FOR_FEEDBACK`:

1. Call `neo_send_feedback` with your reply
2. Call `neo_task_status` again until RUNNING or COMPLETED

## Reconnecting after closing your client

```
neo_list_tasks   →  all tasks with live status + thread IDs
neo_task_status  →  check the task you care about
neo_get_messages →  read output of any COMPLETED task
```

## Postman example sequence

1. **Prompts** tab — run `train-model` to get example task text
2. **Tools** tab — `neo_submit_task` with that message + `workspace` = `NEO_WORKSPACE_DIR`
3. **Tools** tab — `neo_task_status` with returned `thread_id`
4. **Tools** tab — `neo_get_messages` when status is COMPLETED
