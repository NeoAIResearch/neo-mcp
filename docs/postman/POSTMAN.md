# Postman MCP Collection Guide

Use this guide to configure and test Neo MCP in Postman after installing `neo-mcp` 0.5.9+.

## Prerequisites

- Python 3.11+ or `uvx` on PATH
- Neo API key from [heyneo.com/dashboard](https://heyneo.com/dashboard?section=settings#access-keys)

## Postman Environment

Create an environment with:

| Variable | Required | Example |
|----------|----------|---------|
| `NEO_SECRET_KEY` | Yes | `sk-v1-...` |
| `NEO_WORKSPACE_DIR` | Yes | `/Users/you/projects/my-ml-repo` |

`NEO_WORKSPACE_DIR` must be the **absolute project/git root** — not a subdirectory.

## MCP STDIO config

Paste into the Postman MCP request command field:

```json
{
  "mcpServers": {
    "neo": {
      "command": "uvx",
      "args": ["neo-mcp@0.5.9"],
      "env": {
        "NEO_SECRET_KEY": "{{NEO_SECRET_KEY}}",
        "NEO_WORKSPACE_DIR": "{{NEO_WORKSPACE_DIR}}"
      }
    }
  }
}
```

Alternative if `neo-mcp` is installed locally:

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "{{NEO_SECRET_KEY}}",
        "NEO_WORKSPACE_DIR": "{{NEO_WORKSPACE_DIR}}"
      }
    }
  }
}
```

Click **Load Capabilities** (grant STDIO access if prompted).

## Expected capabilities

| Tab | Count | Items |
|-----|-------|-------|
| **Tools** | 12+ | `neo_submit_task`, `neo_task_status`, `neo_get_messages`, ... |
| **Prompts** | 10 | `train-model`, `fine-tune-classifier`, `fine-tune-llm`, `build-rag-pipeline`, `build-ai-agent`, `fix-training-run`, `build-ml-pipeline`, `benchmark-prompts`, `run-eda`, `train-vision-model` |
| **Resources** | 5 | `neo://docs/overview`, `neo://docs/tools`, `neo://docs/workflow`, `neo://docs/env`, `neo://docs/prompts` |

### Prompt args

Most prompts accept an optional workspace-relative **`path`** (e.g. `data/fraud.csv`). Omit it to use a default such as “dataset in the workspace” or `./docs` for RAG. Prompts with **`description`** or **`context`** require that field.

## Suggested collection structure

```
NEO MCP
├── Overview          (collection description — install + links)
├── Connection        (base MCP request with config above)
├── Task lifecycle
│   ├── neo_list_tasks
│   ├── neo_submit_task
│   ├── neo_task_status
│   └── neo_get_messages
├── Example prompts
│   ├── train-model
│   ├── build-rag-pipeline
│   ├── build-ai-agent
│   └── fix-training-run
└── Resources         (run each resource to read docs)
```

## Example workflow

1. **Prompts** → `train-model` → optional `path` = `data/fraud.csv` → **Run** → copy message text
2. **Tools** → `neo_submit_task` → `message` = prompt output, `workspace` = `{{NEO_WORKSPACE_DIR}}` → **Run**
3. Copy `thread_id` from response
4. **Tools** → `neo_task_status` → `thread_id` → **Run** (repeat until COMPLETED)
5. **Tools** → `neo_get_messages` → `thread_id` → **Run**

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Empty Prompts/Resources tabs | Upgrade to neo-mcp 0.5.9+ and **Load Capabilities** again |
| Resource read error (`TextResourceContents`) | Upgrade to 0.5.9+ (uses `ReadResourceContents`) |
| Files land in wrong folder | Set `NEO_WORKSPACE_DIR` to absolute git root |
| `neo_list_tasks` works but submit fails | Check `NEO_SECRET_KEY` and daemon (see `neo-mcp doctor`) |

## Links

- [Neo MCP docs](https://docs.heyneo.com/neo-mcp)
- [Postman MCP docs](https://learning.postman.com/docs/postman-ai/mcp-requests/create/)
