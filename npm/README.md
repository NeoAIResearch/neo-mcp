# test-neo-mcp-daemon

Local execution daemon for [Neo](https://heyneo.so) — written in TypeScript/Node.js, no Python required.

The daemon polls the Neo backend for commands (write files, run subprocesses, list files) and executes them on your machine. It is started automatically by the Neo MCP server when you submit a task.

## Requirements

- Node.js 18+
- A Neo API key (`sk-v1-...`)

## Usage

### Automatic (recommended)

The Neo MCP server starts the daemon automatically when you submit your first task. No manual steps needed.

### Manual start

```bash
NEO_SECRET_KEY=sk-v1-... npx test-neo-mcp-daemon
```

By default the daemon uses your home directory as the workspace. You can pass a path:

```bash
NEO_SECRET_KEY=sk-v1-... npx test-neo-mcp-daemon /path/to/project
```

### Docker

```bash
docker run --rm \
  -e NEO_SECRET_KEY=sk-v1-... \
  -v "$HOME":/root \
  -v "$HOME/.neo":/root/.neo \
  test-neo-mcp-daemon
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NEO_SECRET_KEY` | — | **Required.** Your Neo API key (`sk-v1-...`) |
| `NEO_ENV` | `prod` | Set to `staging` to use the staging backend |
| `NEO_API_URL` | auto | Explicit backend URL override (takes priority over `NEO_ENV`) |
| `NEO_DEPLOYMENT_ID` | auto | Override the deployment UUID (derived from API key by default) |

## How it works

1. Derives a stable deployment UUID from your API key (SHA-256 → UUID v5) — same key always maps to the same UUID, no config files needed
2. Registers with the Neo backend under that UUID
3. Polls `GET /v2/poll/{deployment_id}` for commands
4. Dispatches commands locally: `write_code`, `run_subprocess`, `get_file`, `list_files`, `get_job_status`, `terminate_job`, `create_session`
5. POSTs results back to `POST /v2/poll/response`

## Path safety

The daemon only allows file operations within:
- Your home directory (`~`)
- The system temp directory (`/tmp` on Linux/macOS, `%TEMP%` on Windows)
- The workspace directory passed on the command line

All other absolute paths are blocked.
