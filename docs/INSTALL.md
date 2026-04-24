# Changes to apply to docs.heyneo.com/neo-mcp

Minimum diff. Keep everything else (install command, editor list, structure, copy) exactly as-is on the live docs. Only the three items below.

---

## 1. Swap the command in every MCP config JSON block

**Before:**
```json
"command": "neo-mcp"
```

**After:**
```json
"command": "python3",
"args": ["-m", "neo_mcp"]
```

Apply to every editor section that contains a JSON config (Claude Code, Cursor, Windsurf, VS Code, Zed, Continue, Codex CLI, Antigravity — whichever are on the page).

**Example — full before/after:**

```json
// Before
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
    }
  }
}

// After
{
  "mcpServers": {
    "neo": {
      "command": "python3",
      "args": ["-m", "neo_mcp"],
      "env": { "NEO_SECRET_KEY": "sk-v1-YOUR_KEY" }
    }
  }
}
```

Zed uses a nested `command` object — swap the same way:
```json
// Before
"command": { "path": "neo-mcp", "env": { ... } }

// After
"command": { "path": "python3", "args": ["-m", "neo_mcp"], "env": { ... } }
```

**Codex CLI uses TOML (`~/.codex/config.toml`), not JSON** — apply the same swap to the TOML form:
```toml
# Before
[mcp_servers.neo]
command = "neo-mcp"
[mcp_servers.neo.env]
NEO_SECRET_KEY = "sk-v1-YOUR_KEY"

# After
[mcp_servers.neo]
command = "python3"
args = ["-m", "neo_mcp"]
[mcp_servers.neo.env]
NEO_SECRET_KEY = "sk-v1-YOUR_KEY"
```

## 2. Update the terminal-add commands

neo-mcp is **stdio only** — there's no HTTP/SSE form. Three editors expose a terminal command that registers a stdio subprocess server; the rest are config-file only and are already covered by section 1.

### Claude Code

**Before:**
```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
```

**After:**
```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- python3 -m neo_mcp
```

### Codex CLI

`codex mcp add` takes the same shape as Claude's — name, repeatable `--env` flags, then `--` and the subprocess command.

**Before:**
```bash
codex mcp add neo \
  --env NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
```

**After:**
```bash
codex mcp add neo \
  --env NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- python3 -m neo_mcp
```

This writes the equivalent `[mcp_servers.neo]` block to `~/.codex/config.toml`; editing that file by hand (section 1 TOML block) produces the same result.

### VS Code

`code --add-mcp` takes a single JSON-encoded payload (name + command + args + env) rather than a positional `--` separator.

**Before:**
```bash
code --add-mcp '{"name":"neo","command":"neo-mcp","env":{"NEO_SECRET_KEY":"sk-v1-YOUR_KEY"}}'
```

**After:**
```bash
code --add-mcp '{"name":"neo","command":"python3","args":["-m","neo_mcp"],"env":{"NEO_SECRET_KEY":"sk-v1-YOUR_KEY"}}'
```

### Cursor, Windsurf, Zed, Continue, Antigravity — no terminal add command

Per each tool's official docs (verified on `cursor.com/docs`, `docs.windsurf.com`, `zed.dev/docs`, `docs.continue.dev`, `antigravity.google/docs` as of this writing), there is no `<tool> mcp add` CLI. Register neo-mcp by editing the config file — the JSON/YAML swap from section 1 is the complete instruction. Known file paths:

- **Cursor:** `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per-project) — `mcpServers` key, JSON.
- **Windsurf:** `~/.codeium/windsurf/mcp_config.json` — `mcpServers` key, JSON.
- **Zed:** settings file → `context_servers` key, JSON (nested `command` object — see section 1 Zed note).
- **Continue:** `.continue/mcpServers/*.yaml` — YAML with `type: stdio`, `command`, `args`, `env`.
- **Antigravity:** `mcp_config.json` opened via in-editor **Manage MCP Servers → View raw config** — `mcpServers` key, JSON.

## 3. Add one Windows note

Place immediately after the install command (or just above the first editor config block):

> **Windows:** use `python` instead of `python3` in every command and config below.

## 4. Add an "Integrations (optional)" section

Place at the end of the page (after connection instructions, before troubleshooting). This documents the four integration tools that ship with neo-mcp — they let Neo tasks use the user's GitHub / HuggingFace / Anthropic / OpenRouter keys without re-prompting.

```markdown
## Integrations (optional)

Neo tasks can use your own API keys for GitHub, HuggingFace, Anthropic, and OpenRouter — without you pasting them into every prompt. Keys are stored **on your machine only** (file mode `0o600` at `~/.neo/integrations/`, or your OS keyring) and are never sent to Neo's backend.

From your editor, just ask:

> "Save my OpenRouter key `sk-or-...` for Neo to use"

The agent will call `neo_add_integration` and store the key locally.

**Tools:**

| Tool | What it does |
|---|---|
| `neo_list_integrations` | Shows which providers are configured. Returns names only, never the secret. |
| `neo_add_integration` | Registers a GitHub PAT / HuggingFace token / Anthropic key / OpenRouter key locally. |
| `neo_test_integration` | Calls the provider's API to confirm a stored key still works. |
| `neo_remove_integration` | Deletes a stored key. |

**Env vars Neo tasks automatically receive** once a key is registered:
`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`.

**For laptops — turn on OS keyring (safer):**
```bash
pip install 'neo-mcp[keyring]'
export NEO_INTEGRATIONS_BACKEND=keyring
```

**Full details** (security model, storage locations, bulk provisioning for servers): see [docs/INTEGRATIONS.md on GitHub](https://github.com/heyneo/neo-mcp/blob/main/docs/INTEGRATIONS.md).
```

---

## Why

The `neo-mcp` binary installed by pip isn't on the PATH that Claude Desktop, Cursor, and other GUI editors search — causing `Failed to spawn process: No such file or directory`. Invoking the module via `python3 -m neo_mcp` sidesteps the PATH problem entirely since `python3` is always on the default GUI PATH.

## Do NOT change

- The `pip install neo-mcp` install command.
- The Python version requirement.
- The editor list or section order.
- The API key instructions.
- Any other copy, headings, or structure on the page.
