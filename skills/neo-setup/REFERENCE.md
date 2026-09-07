# Neo MCP setup reference

This file contains details for the [`neo-setup`](SKILL.md) workflow. Read it
when the quick setup path needs client-specific configuration or diagnosis.

## Supported runtime contract

- Python 3.11 or newer.
- Linux, macOS, or Linux under WSL.
- Native Windows PowerShell and `cmd.exe` are unsupported.
- The Python MCP command is `neo-mcp`.
- The default transport for editor integrations is local stdio.
- `NEO_SECRET_KEY` is required for stdio mode.

Do not turn this reference into a second installation method. This skill is
pip-only; do not add npm commands when troubleshooting a Python installation.

## Safe installation variants

Recommended:

```bash
python3 -m pip install --upgrade neo-mcp
```

Isolated installation:

```bash
pipx install --force neo-mcp
```

PEP 668 fallback when a Linux distribution rejects a system install:

```bash
python3 -m pip install --user --break-system-packages neo-mcp
```

Prefer a virtual environment over `--break-system-packages` when the user
already has a project environment. Never suggest `sudo pip`.

Basic checks:

```bash
command -v neo-mcp
python3 -m pip show neo-mcp
python3 --version
```

If the executable is missing after installation:

```bash
python3 -m site --user-base
python3 -m pip show neo-mcp
```

The user-level executable directory is normally the `bin` directory below the
reported user base. Ask the user to add that directory to their shell PATH;
do not silently rewrite shell startup files.

## Client configuration matrix

All examples use a placeholder. Never ask the user to paste a real key into a
chat message or commit one of these files.

### Claude Code

```bash
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-YOUR_KEY \
  -- neo-mcp
claude mcp list
```

Use `--scope project` only when repository-local configuration is intentional.
Start a new Claude Code session after registration.

### Cursor

File: `~/.cursor/mcp.json`

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

Restart Cursor after editing.

### Windsurf

File: `~/.codeium/windsurf/mcp_config.json`

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

Reload Windsurf after editing.

### VS Code with GitHub Copilot

File: `.vscode/mcp.json`

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

### Zed

File: `~/.config/zed/settings.json`

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

Merge this object with existing settings; do not replace the whole file.

### Continue.dev

File: `~/.continue/config.json`

```json
{
  "mcpServers": [
    {
      "name": "neo",
      "transport": {
        "type": "stdio",
        "command": "neo-mcp",
        "env": {
          "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
        }
      }
    }
  ]
}
```

Preserve existing entries in the array.

### OpenAI Codex CLI

File: `~/.codex/config.json`

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

Use the current Codex configuration schema if it differs from this example.

### Generic MCP client

Configure a local stdio server:

```json
{
  "command": "neo-mcp",
  "args": [],
  "env": {
    "NEO_SECRET_KEY": "sk-v1-YOUR_KEY"
  }
}
```

## Diagnostics

Collect these outputs with secret values removed:

```bash
uname -a
python3 --version
command -v neo-mcp
python3 -m pip show neo-mcp
neo-mcp doctor
neo-mcp status
```

For Claude Code, also collect:

```bash
claude mcp list
claude mcp logs neo
```

Do not run `env`, `set`, `ps eww`, or equivalent commands that can dump
`NEO_SECRET_KEY` into logs. If a configuration file must be inspected, show
only the server name, command, arguments, and whether the key variable exists.

## Symptom-to-action guide

| Symptom | Confirm | Safe action |
|---|---|---|
| `neo-mcp: command not found` | `command -v neo-mcp`; `python3 -m pip show neo-mcp` | Install with pip or fix the user's PATH; then recheck the executable. |
| Client reports failed connection | Run `neo-mcp doctor`; inspect client logs | Fix the first startup error, especially missing command or environment. Restart the client. |
| Tools do not appear | Check client reload/session timing | Start a new agent session or reload the client before changing config. |
| `NEO_SECRET_KEY is required` | Confirm the config has an env entry without printing its value | Add `NEO_SECRET_KEY` to the server process environment and restart the client. |
| HTTP 401 | Key is missing, malformed, expired, or revoked | Ask the user to verify or rotate the key in the Neo dashboard. Do not retry repeatedly. |
| HTTP 403 or quota error | Account or plan restriction | Report the backend/account limitation; do not alter local daemon settings. |
| `DAEMON_NOT_RUNNING` | Run `neo-mcp status` and `neo-mcp doctor` | Use detached recovery below, then retry one submission. |
| Readiness or sandbox timeout | Diagnostics show no active local poller | Recover the detached daemon once; if it fails again, report the diagnostic output. |
| Task is running but no local file appears | Check workspace root, daemon status, and evidence | Ensure the submission used the git/project root; inspect evidence before resubmitting. |
| `WORKSPACE_BUSY` | There is an active owner/thread for the workspace | Continue the existing thread; do not submit a competing task. |
| Status and files disagree | Compare backend status with local evidence | Treat local evidence as the source for local execution; call verification for explicit acceptance. |
| Native Windows error | `uname`/runtime indicates native Windows | Stop and move the installation to WSL. Do not invent PowerShell path fixes. |

## Detached daemon recovery

The MCP server normally starts the daemon when the first task is submitted.
Only use recovery after diagnostics show auto-start failed. Keep the daemon
detached from the editor terminal:

```bash
setsid neo-mcp daemon >/dev/null 2>&1 < /dev/null &
```

On macOS, if `setsid` is unavailable, use a detached process mechanism native
to the user's shell and preserve the same properties: no foreground blocking,
no inherited terminal, and no secret in visible arguments. Prefer the built-in
MCP auto-start first.

After recovery, check:

```bash
neo-mcp status
neo-mcp doctor
```

Retry the original submission once. If it fails again, stop retrying and report
the exact sanitized error.

## Workspace and evidence rules

- Pass the git/project root as `workspace`, not a nested source directory.
- Never tell the user that output files are remote. The daemon writes them
  locally.
- `/app/project/path` is an internal backend path; map it to
  `<workspace>/path`.
- `neo_task_status` reports backend lifecycle telemetry and is not independent
  proof that a local command ran.
- Use `neo_get_execution_evidence` for bounded local observations.
- Use `neo_verify_task` for explicit file assertions and bounded acceptance
  commands.
- Do not manually recreate files from backend messages when the daemon should
  have written them.

## Escalation report template

Use this structure:

```text
Setup result: PASS / BLOCKED
Platform and Python: <sanitized result>
Package and executable: <version and command path>
MCP client: <client and config location, without secrets>
Checks passed: <short list>
Checks failed: <short list with exact sanitized errors>
Local evidence: <present / absent / not run>
Backend/account limitation: <none or concise explanation>
Next action: <one safe action>
```
