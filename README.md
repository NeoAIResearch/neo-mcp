# Neo MCP Server

> ⚠️ **REQUIREMENT: The Neo VS Code extension must be open and connected before using this MCP server.**
> If VS Code is not running, tasks will appear to start but will not execute.

## Prerequisites

- Neo account with `x-access-key` (get from the Neo dashboard)
- Docker (recommended) **or** Python 3.11+
- Neo VS Code extension installed and connected

## Install — Docker (recommended)

```bash
claude mcp add --scope user neo -- docker run -i --rm \
  -e NEO_API_KEY -e NEO_SECRET_KEY ghcr.io/heyneo/neo-mcp-server
```

Set both keys in your environment before starting Claude Code:

```bash
export NEO_API_KEY=ak-v1-...    # access key from Neo dashboard
export NEO_SECRET_KEY=sk-v1-... # secret key from Neo dashboard
```

## Install — Python

```bash
pip install git+https://github.com/NeoResearchAI/MCPServer.git#subdirectory=neo-mcp
```

```bash
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-access-key -e NEO_SECRET_KEY=your-secret-key \
  -- neo-mcp
```

Set both `NEO_API_KEY` and `NEO_SECRET_KEY` in your environment before starting Claude Code.

## Configuration scopes

| Scope     | Flag              | When to use                                      |
|-----------|-------------------|--------------------------------------------------|
| `user`    | `--scope user`    | Personal use across all projects (recommended)   |
| `project` | `--scope project` | Share with your team via `.mcp.json` in the repo |
| `local`   | *(default)*       | One project, not committed                       |

## Available tools

| Tool                | Description                                                        |
|---------------------|--------------------------------------------------------------------|
| `neo_submit_task`   | Submit a task to Neo. Blocks until complete and returns the full result. |
| `neo_task_status`   | Poll task status. Poll every 10–15 s while `RUNNING`.              |
| `neo_get_messages`  | Read the full output once status is `COMPLETED`.                   |
| `neo_send_feedback` | Reply to Neo when status is `WAITING_FOR_FEEDBACK`.                |
| `neo_pause_task`    | Pause a running task.                                              |
| `neo_resume_task`   | Resume a paused task.                                              |
| `neo_stop_task`     | Stop and clean up a task.                                          |

## Example usage

In a Claude Code session, try:

- `"Use Neo to build a churn model on my churn.csv, optimise for recall"`
- `"Check the status of Neo task thr_abc123"`
- `"Ask Neo to analyse my dataset and suggest the best model"`

## Read-only mode

Set `NEO_READ_ONLY=true` to expose only `neo_task_status` and `neo_get_messages`.
Write tools (`neo_submit_task`, `neo_send_feedback`, `neo_pause_task`, `neo_resume_task`, `neo_stop_task`) will not be registered.

Set `NEO_WORKSPACE_DIR=/your/project` when running in Docker so Neo knows the correct path to your project files.

```bash
docker run -i --rm -e NEO_API_KEY -e NEO_SECRET_KEY -e NEO_READ_ONLY=true ghcr.io/heyneo/neo-mcp-server
```
