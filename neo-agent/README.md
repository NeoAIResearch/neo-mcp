# Neo Agent (Go Binary)

Standalone local MCP server and command poller.

## What it does
- Exposes MCP tools over STDIO (`initialize`, `tools/list`, `tools/call`)
- Polls remote API (`GET /api/commands`) on a timer
- Executes local actions and posts results (`POST /api/result`)
- Writes files on the user's machine under a restricted workspace root

## Env vars
- `NEO_TOKEN` or `NEO_SECRET_KEY` (required for poller auth)
- `NEO_SERVER` (default `https://heyneo.so`)
- `NEO_POLL_INTERVAL` seconds (default `5`)
- `NEO_WORKSPACE` (default `~/neo-workspace`)
- `NEO_DEPLOYMENT_ID` (optional override; else derived from token)

## Security defaults in this implementation
- Path operations are restricted to `NEO_WORKSPACE`
- Path traversal outside workspace is blocked
- Relative command execution uses workspace as CWD

## Build
```bash
cd neo-agent
make build
```

Cross platform:
```bash
make build-all
```

## MCP config example (mac/linux)
```json
{
  "mcpServers": {
    "neo": {
      "command": "~/.neo/agent",
      "type": "stdio",
      "env": {
        "NEO_TOKEN": "YOUR_TOKEN",
        "NEO_SERVER": "https://heyneo.so",
        "NEO_WORKSPACE": "~/neo-workspace"
      }
    }
  }
}
```

## Notes
- This repo environment does not have Go installed, so compile verification must run on your machine/CI.
- If you want full parity with existing Neo backend routing (`/v2/poll/{deployment_id}`), we can switch poller endpoints from `/api/*` to `/v2/*` in a follow-up patch.
