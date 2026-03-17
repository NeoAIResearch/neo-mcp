# Neo MCP — Registration Guide

> ⚠️ The Neo VS Code extension must be open and connected before using this server.

---

## Do I need to clone this repo?

**No.** There are three ways to run the server — pick the one that suits you:

| Method | Requires | Best for |
|---|---|---|
| **Docker** | Docker installed | Everyone — no Python, no repo |
| **pip install** | Python 3.11+ | Python users who prefer a clean install |
| **Clone + run** | Python 3.11+ + git | Contributors / local development |

---

## Option 1 — Docker (recommended, no repo needed)

```bash
docker pull ghcr.io/heyneo/neo-mcp-server
```

That's it. The image is self-contained. Register it with your LLM client using the commands below.

---

## Option 2 — pip install (no repo needed)

```bash
pip install git+https://github.com/NeoResearchAI/MCPServer.git#subdirectory=neo-mcp
```

This installs a `neo-mcp` command globally. Verify:

```bash
neo-mcp
```

(Starts the server; Ctrl+C to exit. It will fail immediately without keys set, which confirms it is installed correctly.)

---

## Option 3 — Clone and run

```bash
git clone https://github.com/NeoResearchAI/MCPServer.git
cd MCPServer/neo-mcp
pip install -r requirements.txt
```

Run directly:
```bash
export NEO_API_KEY=your-access-key
export NEO_SECRET_KEY=your-secret-key
python3 src/neo_mcp/server.py
```

---

## Required environment variables

```
NEO_API_KEY=ak-v1-...       # access key from Neo dashboard
NEO_SECRET_KEY=sk-v1-...    # secret key from Neo dashboard
```

The server auto-discovers your VS Code sandbox ID from `~/.neo/daemon/daemon.log` — no extra config needed.

---

## Register with your LLM client

### Claude Code (CLI)

**Docker**
```bash
claude mcp add --scope user neo -e NEO_API_KEY=your-access-key -e NEO_SECRET_KEY=your-secret-key -- docker run -i --rm -e NEO_API_KEY -e NEO_SECRET_KEY -v ~/.neo:/root/.neo:ro ghcr.io/heyneo/neo-mcp-server
```

**pip install**
```bash
claude mcp add --scope user neo -e NEO_API_KEY=your-access-key -e NEO_SECRET_KEY=your-secret-key -- neo-mcp
```

**Clone**
```bash
claude mcp add --scope user neo -e NEO_API_KEY=your-access-key -e NEO_SECRET_KEY=your-secret-key -- python3 /absolute/path/to/MCPServer/src/neo_mcp/server.py
```

**Scopes — where your config is saved and who can see it:**

| Scope | Flag | Saved to | Who sees it | When to use |
|---|---|---|---|---|
| user | `--scope user` | `~/.claude/settings.json` | Only you, all projects | Personal use across all your work |
| project | `--scope project` | `.mcp.json` in repo | Everyone who clones the repo | Team-shared setup |
| local | *(no flag)* | `.claude/settings.local.json` | Only you, this project | Testing in one project, not committed |

For most users, **`--scope user`** is the right choice — register once, Neo is available everywhere without touching any repo files. Use `--scope project` if you want teammates to automatically get the Neo MCP when they clone the repo (they still need to set their own `NEO_API_KEY` and `NEO_SECRET_KEY`).

**Verify:**
```bash
claude mcp list
claude mcp logs neo      # view server logs if something breaks
```

---

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

**Docker**
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

**pip install**
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
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

### Cursor

Open **Settings → MCP** (or edit `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
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

### Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
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

### Cline (VS Code extension)

Open the Cline panel → **MCP Servers → Add Server → Manually edit config**:

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_API_KEY": "your-access-key",
        "NEO_SECRET_KEY": "your-secret-key"
      }
    }
  }
}
```

---

### Continue.dev

Edit `~/.continue/config.json`:

```json
{
  "experimental": {
    "modelContextProtocolServers": [
      {
        "transport": {
          "type": "stdio",
          "command": "neo-mcp",
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
| `NEO_DEPLOYMENT_ID` | *(auto-discovered)* | Override the auto-discovered sandbox ID |
| `NEO_API_URL` | `https://master.heyneo.so` | Override backend URL |
| `NEO_READ_ONLY` | `false` | `true` to expose only status + read tools |
| `NEO_WORKSPACE_DIR` | *(auto: CWD)* | Override working directory sent to Neo (useful in Docker) |
