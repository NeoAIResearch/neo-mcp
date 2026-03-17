# Neo MCP — Registration Guide

Two environment variables are required for all clients:

```
NEO_API_KEY=ak-v1-...       # access key from Neo dashboard
NEO_SECRET_KEY=sk-v1-...    # secret key from Neo dashboard
```

The server auto-discovers your VS Code sandbox ID from `~/.neo/daemon/daemon.log`.
Set `NEO_DEPLOYMENT_TYPE=cloud` if you want cloud mode instead of VS Code mode.

---

## Claude Code (CLI)

**Python (local)**
```bash
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-access-key \
  -e NEO_SECRET_KEY=your-secret-key \
  -- python3 /absolute/path/to/neo-mcp/server.py
```

**Docker (published image)**
```bash
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-access-key \
  -e NEO_SECRET_KEY=your-secret-key \
  -- docker run -i --rm \
     -e NEO_API_KEY -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo:ro \
     ghcr.io/heyneo/neo-mcp-server
```

> `-v ~/.neo:/root/.neo:ro` mounts the Neo daemon directory so the container can auto-discover the sandbox ID.

**Scopes:**
- `--scope user` — available in all your projects (recommended for personal use)
- `--scope project` — saved to `.mcp.json` in the repo, shared with your team
- *(no flag)* — local only, not committed

**Verify it registered:**
```bash
claude mcp list
claude mcp logs neo      # view server logs if something breaks
```

---

## Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "neo": {
      "command": "python3",
      "args": ["/absolute/path/to/neo-mcp/server.py"],
      "env": {
        "NEO_API_KEY": "your-access-key",
        "NEO_SECRET_KEY": "your-secret-key"
      }
    }
  }
}
```

**Docker variant:**
```json
{
  "mcpServers": {
    "neo": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "NEO_API_KEY",
        "-e", "NEO_SECRET_KEY",
        "-v", "/Users/you/.neo:/root/.neo:ro",
        "ghcr.io/heyneo/neo-mcp-server"
      ],
      "env": {
        "NEO_API_KEY": "your-access-key",
        "NEO_SECRET_KEY": "your-secret-key"
      }
    }
  }
}
```

Restart Claude Desktop after editing.

---

## Cursor

Open **Settings → MCP** (or edit `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "neo": {
      "command": "python3",
      "args": ["/absolute/path/to/neo-mcp/server.py"],
      "env": {
        "NEO_API_KEY": "your-access-key",
        "NEO_SECRET_KEY": "your-secret-key"
      }
    }
  }
}
```

Reload the window after saving (`Cmd+Shift+P` → `Developer: Reload Window`).

---

## Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "python3",
      "args": ["/absolute/path/to/neo-mcp/server.py"],
      "env": {
        "NEO_API_KEY": "your-access-key",
        "NEO_SECRET_KEY": "your-secret-key"
      }
    }
  }
}
```

Restart Windsurf after saving.

---

## Cline (VS Code extension)

Open the Cline panel → **MCP Servers → Add Server → Manually edit config**,
then add to the config:

```json
{
  "mcpServers": {
    "neo": {
      "command": "python3",
      "args": ["/absolute/path/to/neo-mcp/server.py"],
      "env": {
        "NEO_API_KEY": "your-access-key",
        "NEO_SECRET_KEY": "your-secret-key"
      }
    }
  }
}
```

---

## Continue.dev

Edit `~/.continue/config.json`, add to the `mcpServers` array:

```json
{
  "experimental": {
    "modelContextProtocolServers": [
      {
        "transport": {
          "type": "stdio",
          "command": "python3",
          "args": ["/absolute/path/to/neo-mcp/server.py"],
          "env": {
            "NEO_API_KEY": "your-access-key",
            "NEO_SECRET_KEY": "your-secret-key"
          }
        }
      }
    ]
  }
}
```

---

## Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `NEO_API_KEY` | *(required)* | Access key from Neo dashboard |
| `NEO_SECRET_KEY` | *(required)* | Secret key from Neo dashboard |
| `NEO_DEPLOYMENT_TYPE` | `vscode` | `vscode` or `cloud` |
| `NEO_DEPLOYMENT_ID` | *(auto-discovered)* | Pin to a specific sandbox ID |
| `NEO_API_URL` | `https://master.heyneo.so` | Override backend URL |
| `NEO_READ_ONLY` | `false` | `true` to expose only status + read tools |
