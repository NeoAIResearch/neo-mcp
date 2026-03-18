# Neo MCP — Registration Guide

> The Neo VS Code extension must be open and connected before submitting tasks.

---

## Fastest path — setup wizard

```bash
pip install neo-mcp
neo-mcp setup
```

The wizard:
1. Prompts for your `NEO_API_KEY` and `NEO_SECRET_KEY` (reads env if already set)
2. Lets you pick which editors to configure
3. For Cursor, Windsurf, and VS Code: offers "Remote (hosted, no install)" vs "Local (stdio)"
4. Writes all config files and backs up existing ones (`.bak`)
5. Prints verification steps

**Non-interactive / scripted install:**

```bash
neo-mcp setup \
  --api-key ak-v1-... \
  --secret-key sk-v1-... \
  --editor claude,cursor \
  --remote \
  --no-backup
```

Flags: `--editor`, `--api-key`, `--secret-key`, `--remote`, `--scope`, `--no-backup`

---

## Do I need to clone this repo?

**No.** Three install methods are available:

| Method | Requires | Best for |
|---|---|---|
| **pip install** | Python 3.11+ | Quickest local install |
| **Docker** | Docker installed | No Python required |
| **Clone + run** | Python 3.11+ + git | Contributors / local development |

---

## Install

### pip (PyPI — recommended)

```bash
pip install neo-mcp
```

### Docker

```bash
docker pull ghcr.io/heyneo/neo-mcp-server
```

### pip from GitHub

```bash
pip install git+https://github.com/NeoResearchAI/MCPServer.git#subdirectory=neo-mcp
```

### Clone

```bash
git clone https://github.com/NeoResearchAI/MCPServer.git
cd MCPServer/neo-mcp
pip install -r requirements.txt
```

---

## Required environment variables

```
NEO_API_KEY=ak-v1-...       # access key from Neo dashboard
NEO_SECRET_KEY=sk-v1-...    # secret key from Neo dashboard
```

---

## Manual registration — Claude Code (CLI)

**Remote (no local install required)**
```bash
claude mcp add --transport http neo https://mcp.heyneo.so/mcp \
  --header "x-access-key: YOUR_NEO_API_KEY" \
  --header "Authorization: Bearer YOUR_NEO_SECRET_KEY"
```

**Local — pip**
```bash
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-access-key -e NEO_SECRET_KEY=your-secret-key \
  -- neo-mcp
```

**Local — Docker**
```bash
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-access-key -e NEO_SECRET_KEY=your-secret-key \
  -- docker run -i --rm -e NEO_API_KEY -e NEO_SECRET_KEY \
     -v ~/.neo:/root/.neo:ro ghcr.io/heyneo/neo-mcp-server
```

**Local — Clone**
```bash
claude mcp add --scope user neo \
  -e NEO_API_KEY=your-access-key -e NEO_SECRET_KEY=your-secret-key \
  -- python3 /absolute/path/to/MCPServer/neo-mcp/src/neo_mcp/server.py
```

**Scopes:**

| Scope | Flag | Saved to | When to use |
|---|---|---|---|
| user | `--scope user` | `~/.claude/settings.json` | Personal use across all projects |
| project | `--scope project` | `.mcp.json` in repo | Team-shared setup |
| local | *(no flag)* | `.claude/settings.local.json` | One project, not committed |

```bash
claude mcp list
claude mcp logs neo
```

---

## Manual registration — Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

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

## Manual registration — Cursor

`~/.cursor/mcp.json`:

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

## Manual registration — Windsurf

`~/.codeium/windsurf/mcp_config.json`:

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

## Manual registration — Cline (VS Code extension)

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

## Manual registration — Continue.dev

`~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "neo",
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
