# Neo MCP Overview

Neo MCP is the [Model Context Protocol](https://modelcontextprotocol.io) server that connects Neo — an autonomous AI engineer for ML, LLM, and data workflows — to your editor or MCP client.

## What Neo does

- Submit AI/ML tasks in plain English (training, fine-tuning, RAG, agents, pipelines)
- Run workloads via a local daemon that writes files to **your machine**
- Poll status, read output, pause/resume/stop tasks
- Store third-party credentials locally (GitHub, HuggingFace, Anthropic, etc.)

**Local-first:** code, models, metrics, and reports land in your repo. Nothing is stored remotely.

## Links

- [Neo](https://heyneo.com)
- [Neo MCP docs](https://docs.heyneo.com/neo-mcp)
- [API keys](https://heyneo.com/dashboard?section=settings#access-keys)

## Postman setup

Set `NEO_SECRET_KEY` and `NEO_WORKSPACE_DIR` (absolute project/git root) in your Postman Environment, then **Load Capabilities**.
