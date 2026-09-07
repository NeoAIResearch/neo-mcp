---
name: neo-setup
description: Install and configure the Python neo-mcp server for MCP clients, verify tool discovery and local execution, and diagnose setup, authentication, daemon, workspace, and platform failures. Use when a user asks to set up neo-mcp, connect Neo to a coding agent, or troubleshoot an existing installation.
user-invocable: true
metadata: {"openclaw": {"emoji": "🔧", "os": ["darwin", "linux"]}}
---

# Neo MCP Setup

Use this skill when a user needs the Python `neo-mcp` package installed and connected to an MCP-enabled coding agent. The workflow is pip-only and supports Linux, macOS, and WSL.

## Non-negotiable behavior

- Do not provide npm installation commands. Configure the Python package and `neo-mcp` executable only.
- Native Windows PowerShell and `cmd.exe` are unsupported because the Python daemon uses POSIX shell and container-path semantics. If the user is on native Windows, explain that WSL is required and stop before installing.
- Never print, echo, log, or paste a user's `NEO_SECRET_KEY`. Redact it from command output and reports. Never ask the user to commit an MCP config containing the key.
- Make the smallest necessary change. Back up an existing client config before editing it, preserve unrelated servers, and tell the user which file changed.
- Use the user's explicit project path when provided. Otherwise use `git rev-parse --show-toplevel`; only fall back to the current directory when no repository exists.
- Do not claim that backend status proves local execution. Use `neo_get_execution_evidence` and `neo_verify_task` when local proof is required.

## Setup workflow

Follow these steps in order. Do not skip verification.

### 1. Check platform and project root

Run read-only checks:

```bash
uname -s
python3 --version
git rev-parse --show-toplevel 2>/dev/null || pwd
```

Require Python 3.11 or newer. On native Windows, report:

> Native Windows execution is unsupported. Install and run Neo inside WSL, then configure the MCP client to use the WSL `neo-mcp` executable.

### 2. Install the Python package

Prefer an isolated install:

```bash
python3 -m pip install --upgrade neo-mcp
# Or, if pipx is already installed:
pipx install --force neo-mcp
```

If Linux returns `externally-managed-environment`, do not use `sudo pip`. Use a virtual environment or:

```bash
python3 -m pip install --user --break-system-packages neo-mcp
```

Check the executable without exposing credentials:

```bash
command -v neo-mcp
python3 -m pip show neo-mcp
```

If `command -v` returns nothing after a user install, inspect `python3 -m site --user-base` and add its `bin` directory to the user's PATH. Do not hard-code a guessed path into an editor config.

### 3. Collect the key safely

Ask the user to obtain a key from the Neo dashboard. Accept it through the MCP client's environment configuration or an interactive setup command; do not ask them to send it in chat. The configured value must be available as `NEO_SECRET_KEY` to the server process.

The key belongs in the editor's private MCP configuration. Do not put it in project source, committed `.env` files, shell history, screenshots, or diagnostics.

### 4. Configure the MCP client

Use the relevant pip configuration below. Replace `sk-v1-YOUR_KEY` locally; never include a real key in a response.

#### Claude Code

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
```

Use `--scope project` only when the user explicitly wants a repository-local `.mcp.json`. Open a new Claude Code session after registration.

#### Cursor

Edit `~/.cursor/mcp.json` or use Cursor's MCP settings:

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Restart Cursor after editing the file.

#### Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "neo": {
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Reload Windsurf after saving if the server does not appear.

#### VS Code with GitHub Copilot

Create or edit `.vscode/mcp.json`:

```json
{
  "servers": {
    "neo": {
      "type": "stdio",
      "command": "neo-mcp",
      "env": {
        "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
      }
    }
  }
}
```

Use Copilot Agent mode and reload the window.

#### Zed

Add a custom context server named `neo` to `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "neo": {
      "source": "custom",
      "command": {
        "path": "neo-mcp",
        "args": [],
        "env": {
          "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
        }
      }
    }
  }
}
```

#### Continue, Codex CLI, and other MCP clients

Use the client's stdio MCP configuration with:

- command: `neo-mcp`
- arguments: none
- environment: `NEO_SECRET_KEY=sk-v1-YOUR_KEY`

For client-specific config shapes, read [`REFERENCE.md`](REFERENCE.md) and the client's current documentation. Do not substitute a hosted HTTP server or an npm command in this pip-only flow.

### 5. Verify registration

For Claude Code:

```bash
claude mcp list
```

Then start a new agent session and ask it to list the Neo tools. Expected core tools include `neo_submit_task`, `neo_task_status`, `neo_get_messages`, `neo_get_execution_evidence`, and `neo_verify_task`.

If tools are missing, reload or restart the client before changing the installation.

### 6. Run a bounded smoke test

Use a disposable or user-approved project root. Submit one small task that creates a clearly named file, then follow the lifecycle:

1. Call `neo_submit_task` with the project root as `workspace`.
2. Poll `neo_task_status` at a reasonable interval; never tight-loop.
3. When complete, call `neo_get_messages`.
4. Confirm the expected file exists locally.
5. If proof matters, call `neo_get_execution_evidence` and `neo_verify_task` with bounded file assertions or acceptance commands.

Explain that the daemon runs locally and writes output files into the local workspace. Paths such as `/app/project/...` in agent messages are backend container paths and are remapped to the local project root.

## Debugging workflow

Diagnose from evidence, one layer at a time:

1. **Platform:** confirm Linux, macOS, or WSL; stop on native Windows.
2. **Executable:** check `command -v neo-mcp`, `python3 --version`, and `python3 -m pip show neo-mcp`.
3. **Registration:** inspect the client config or `claude mcp list`; preserve and redact secrets.
4. **Server startup:** run `neo-mcp doctor` and `neo-mcp status`; capture errors without the key.
5. **Authentication:** distinguish missing key, 401/403, quota, and network errors. Never retry a rejected key indefinitely.
6. **Daemon/readiness:** if submission reports `DAEMON_NOT_RUNNING` or sandbox readiness failure, use the documented detached-daemon recovery in [`REFERENCE.md`](REFERENCE.md), then retry once.
7. **Workspace:** confirm the path is the project/git root and that the local user can write there. Never advise writing outside the approved workspace.
8. **Task state:** use `neo_task_status`, then messages and local evidence; do not treat stale backend plans as proof.

Common responses:

- `neo-mcp: command not found`: installation or PATH problem; fix that before editing MCP JSON.
- Tools absent: restart the agent session/client; tools load at session start for many clients.
- `NEO_SECRET_KEY is required`: the client did not pass the environment variable; fix the config and restart.
- 401/403: validate the key/account with the dashboard; do not change workspace or daemon settings to mask an account failure.
- `DAEMON_NOT_RUNNING`, readiness timeout, or no local files: run diagnostics, recover the detached daemon once, and retry once.
- Workspace busy: inspect existing tasks and continue the owning thread; do not submit competing tasks in the same workspace.
- Native Windows: move the installation to WSL; do not try quoting or path workarounds in PowerShell.

For the detailed command matrix and recovery steps, read [`REFERENCE.md`](REFERENCE.md).

## Completion report

Tell the user:

- What platform and Python version were detected.
- Which install command and MCP client config were used.
- Which verification checks passed or failed.
- Whether the smoke task produced local evidence.
- Any remaining backend/account limitation and the single next action.

Never include the API key, full secret-bearing config, or unredacted logs.

The daemon auto-starts on the first task submission. Do not ask the user to run it manually unless diagnostics show auto-start failed; use the detached recovery documented in the reference guide.
