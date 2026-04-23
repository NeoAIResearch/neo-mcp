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

## 2. Update the Claude Code CLI command

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

## 3. Add one Windows note

Place immediately after the install command (or just above the first editor config block):

> **Windows:** use `python` instead of `python3` in every command and config below.

---

## Why

The `neo-mcp` binary installed by pip isn't on the PATH that Claude Desktop, Cursor, and other GUI editors search — causing `Failed to spawn process: No such file or directory`. Invoking the module via `python3 -m neo_mcp` sidesteps the PATH problem entirely since `python3` is always on the default GUI PATH.

## Do NOT change

- The `pip install neo-mcp` install command.
- The Python version requirement.
- The editor list or section order.
- The API key instructions.
- Any other copy, headings, or structure on the page.
