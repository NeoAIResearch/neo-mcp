# Neo MCP — Client Setup Guide

The fastest way to configure any editor is the setup wizard:

```bash
pip install neo-mcp
neo-mcp setup
```

The wizard auto-detects installed editors, prompts for your secret key, and writes all configs.
Manual steps are documented below for reference.

---

## Claude Code

### Remote (no local install)
```bash
claude mcp add --transport http neo https://mcp.heyneo.so/mcp \
  --header "Authorization: Bearer YOUR_NEO_SECRET_KEY"
```

### Local — pip
```bash
pip install neo-mcp
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-... \
  -- neo-mcp
```

### Local — Docker
```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-... \
  -- docker run -i --rm -e NEO_SECRET_KEY \
     ghcr.io/heyneo/neo-mcp-server
```

---

## Cursor

Config file: `~/.cursor/mcp.json`
(Create it if it doesn't exist. Cursor must be restarted after editing.)

### Remote
```json
{
  "mcpServers": {
    "neo": {
      "url": "https://mcp.heyneo.so/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_NEO_SECRET_KEY"
      }
    }
  }
}
```

### Local — pip
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

### Local — Docker
```json
{
  "mcpServers": {
    "neo": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "NEO_SECRET_KEY",
        "ghcr.io/heyneo/neo-mcp-server"
      ],
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

---

## Windsurf

Config file: `~/.codeium/windsurf/mcp_config.json`

### Remote
```json
{
  "mcpServers": {
    "neo": {
      "serverUrl": "https://mcp.heyneo.so/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_NEO_SECRET_KEY"
      }
    }
  }
}
```

### Local — pip
```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

---

## Zed

Config file: `~/.config/zed/settings.json` — add under the `"context_servers"` key.

### Remote (via mcp-remote proxy)
```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "npx",
        "args": [
          "-y", "mcp-remote",
          "https://mcp.heyneo.so/mcp",
          "--header", "Authorization:Bearer YOUR_NEO_SECRET_KEY"
        ]
      }
    }
  }
}
```
> Zed does not yet support native HTTP MCP transport — `mcp-remote` is a lightweight
> proxy that bridges HTTP → stdio. Install once: `npm install -g mcp-remote`

### Local — pip
```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "neo-mcp",
        "args": [],
        "env": {
          "NEO_SECRET_KEY": "sk-v1-..."
        }
      }
    }
  }
}
```

---

## VS Code (GitHub Copilot / Continue)

### GitHub Copilot (VS Code 1.99+)

Workspace config `.vscode/mcp.json`:

#### Remote
```json
{
  "servers": {
    "neo": {
      "type": "http",
      "url": "https://mcp.heyneo.so/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_NEO_SECRET_KEY"
      }
    }
  }
}
```

#### Local stdio
```json
{
  "servers": {
    "neo": {
      "type": "stdio",
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

### Continue.dev

Config file: `~/.continue/config.json` — add under `"mcpServers"`:

```json
{
  "mcpServers": [
    {
      "name": "neo",
      "transport": {
        "type": "stdio",
        "command": "neo-mcp",
        "env": {
          "NEO_SECRET_KEY": "sk-v1-..."
        }
      }
    }
  ]
}
```

---

## OpenAI Codex CLI

Config file: `~/.codex/config.json`

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-..."
      }
    }
  }
}
```

---

## Summary — transport support per editor

| Editor | Local stdio | Remote HTTP |
|---|---|---|
| Claude Code | ✅ | ✅ native |
| Cursor | ✅ | ✅ native |
| Windsurf | ✅ | ✅ native |
| Zed | ✅ | ✅ via `mcp-remote` proxy |
| VS Code Copilot | ✅ | ✅ native (v1.99+) |
| Continue.dev | ✅ | ⚠️ stdio only for now |
| OpenAI Codex CLI | ✅ | ❌ stdio only |

---

## Where are the keys?

Get your Neo secret key from the **Neo dashboard**:
- `NEO_SECRET_KEY` — secret key (starts with `sk-v1-`)
