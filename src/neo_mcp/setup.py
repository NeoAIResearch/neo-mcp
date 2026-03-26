"""neo-mcp setup wizard — stdlib only."""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REMOTE_URL = "https://mcpserver.heyneo.com/mcp"

EDITORS = [
    ("claude", "Claude Code"),
    ("cursor", "Cursor"),
    ("windsurf", "Windsurf"),
    ("zed", "Zed"),
    ("vscode", "VS Code (GitHub Copilot)"),
    ("continue", "Continue.dev"),
    ("codex", "OpenAI Codex CLI"),
]

_SUPPORTS_REMOTE = {"claude", "cursor", "windsurf", "vscode"}


def _parse_args(args: list) -> dict:
    opts: dict = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--secret-key", "--secret_key") and i + 1 < len(args):
            opts["secret_key"] = args[i + 1]; i += 2
        elif a == "--editor" and i + 1 < len(args):
            opts["editor"] = args[i + 1]; i += 2
        elif a == "--remote":
            opts["remote"] = True; i += 1
        elif a == "--no-backup":
            opts["no_backup"] = True; i += 1
        elif a == "--scope" and i + 1 < len(args):
            opts["scope"] = args[i + 1]; i += 2
        else:
            i += 1
    return opts


def _prompt_editors() -> list:
    print("\nWhich editors do you want to configure?")
    for idx, (key, label) in enumerate(EDITORS, 1):
        print(f"  {idx}. {label}")
    print("  0. All")
    raw = input("\nEnter numbers separated by commas (e.g. 1,2): ").strip()
    if raw == "0":
        return [k for k, _ in EDITORS]
    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(EDITORS):
                selected.append(EDITORS[idx][0])
    return selected or [EDITORS[0][0]]


def _ask_remote() -> bool:
    ans = input("Use remote hosted server (mcpserver.heyneo.com)? [Y/n]: ").strip().lower()
    return ans in ("", "y", "yes")


def _backup(path: Path, no_backup: bool) -> None:
    if not no_backup and path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)


def _strip_jsonc(text: str) -> str:
    """Strip JS-style comments from JSONC, respecting string boundaries.

    A naive regex strips // inside URL values like "https://example.com",
    corrupting the JSON. This state-machine approach only strips comments
    that appear outside of quoted strings.
    """
    result: list[str] = []
    i = 0
    in_string = False
    while i < len(text):
        c = text[i]
        if in_string:
            result.append(c)
            if c == "\\":
                i += 1
                if i < len(text):
                    result.append(text[i])
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
                result.append(c)
            elif c == "/" and i + 1 < len(text):
                nxt = text[i + 1]
                if nxt == "/":
                    while i < len(text) and text[i] != "\n":
                        i += 1
                    continue
                elif nxt == "*":
                    i += 2
                    while i < len(text) - 1 and not (text[i] == "*" and text[i + 1] == "/"):
                        i += 1
                    i += 2
                    continue
                else:
                    result.append(c)
            else:
                result.append(c)
        i += 1
    return "".join(result)


def _read_json_file(path: Path) -> dict:
    """Read a JSON or JSONC file (e.g. VS Code settings with // comments)."""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # File may be JSONC (comments allowed) — strip comments and retry
        return json.loads(_strip_jsonc(text))


def _write_json_file(path: Path, data: dict, no_backup: bool) -> None:
    _backup(path, no_backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _merge_mcp_servers(path: Path, key: str, server_cfg: dict, no_backup: bool) -> None:
    data = _read_json_file(path) if path.exists() else {}
    data.setdefault("mcpServers", {})[key] = server_cfg
    _write_json_file(path, data, no_backup)


def _configure_claude(secret_key: str, opts: dict) -> tuple:
    use_remote = opts.get("remote", False)

    scope = opts.get("scope", "user")
    no_backup = opts.get("no_backup", False)

    if shutil.which("claude"):
        try:
            if use_remote:
                cmd = [
                    "claude", "mcp", "add", "--transport", "http",
                    "--scope", scope, "neo", REMOTE_URL,
                    "--header", f"Authorization: Bearer {secret_key}",
                ]
            else:
                cmd = [
                    "claude", "mcp", "add", "--scope", scope,
                    "-e", f"NEO_SECRET_KEY={secret_key}",
                    "--", "neo-mcp",
                ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                mode = "remote" if use_remote else "local"
                return True, f"Configured via claude CLI ({mode}, scope={scope})"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Fallback: write JSON directly to ~/.claude/claude_desktop_config.json
    if use_remote:
        server_cfg: dict = {
            "transport": "http",
            "url": REMOTE_URL,
            "headers": {
                "Authorization": f"Bearer {secret_key}",
            },
        }
    else:
        server_cfg = {
            "command": "neo-mcp",
            "env": {"NEO_SECRET_KEY": secret_key},
        }

    # Claude Desktop fallback path
    if sys.platform == "darwin":
        fallback = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "win32":
        fallback = Path(os.environ.get("APPDATA", "~")) / "Claude" / "claude_desktop_config.json"
    else:
        fallback = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

    try:
        _merge_mcp_servers(fallback, "neo", server_cfg, no_backup)
        mode = "remote" if use_remote else "local"
        return True, f"Written to {fallback} ({mode}) — restart Claude"
    except OSError:
        config_json = json.dumps({"mcpServers": {"neo": server_cfg}}, indent=2)
        print(f"\n  `claude` CLI not found and could not write config. Paste this manually:")
        print(config_json)
        return False, "claude CLI not found — printed config to stdout"


def _configure_cursor(secret_key: str, opts: dict) -> tuple:
    use_remote = opts.get("remote", False)

    path = Path.home() / ".cursor" / "mcp.json"
    no_backup = opts.get("no_backup", False)

    if use_remote:
        server_cfg: dict = {
            "url": REMOTE_URL,
            "headers": {
                "Authorization": f"Bearer {secret_key}",
            },
        }
    else:
        server_cfg = {
            "command": "neo-mcp",
            "env": {"NEO_SECRET_KEY": secret_key},
        }

    try:
        _merge_mcp_servers(path, "neo", server_cfg, no_backup)
        mode = "remote" if use_remote else "local"
        return True, f"Written to {path} ({mode})"
    except OSError as e:
        print(f"\n  Could not write {path}: {e}. Paste this manually:")
        print(json.dumps({"mcpServers": {"neo": server_cfg}}, indent=2))
        return False, "Write failed — printed config to stdout"


def _configure_windsurf(secret_key: str, opts: dict) -> tuple:
    use_remote = opts.get("remote", False)

    path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    no_backup = opts.get("no_backup", False)

    if use_remote:
        server_cfg: dict = {
            "serverUrl": REMOTE_URL,
            "headers": {
                "Authorization": f"Bearer {secret_key}",
            },
        }
    else:
        server_cfg = {
            "command": "neo-mcp",
            "env": {"NEO_SECRET_KEY": secret_key},
        }

    try:
        _merge_mcp_servers(path, "neo", server_cfg, no_backup)
        mode = "remote" if use_remote else "local"
        return True, f"Written to {path} ({mode})"
    except OSError as e:
        print(f"\n  Could not write {path}: {e}. Paste this manually:")
        print(json.dumps({"mcpServers": {"neo": server_cfg}}, indent=2))
        return False, "Write failed — printed config to stdout"


def _configure_zed(secret_key: str, opts: dict) -> tuple:
    use_remote = opts.get("remote", False)

    path = Path.home() / ".config" / "zed" / "settings.json"
    no_backup = opts.get("no_backup", False)

    if use_remote:
        server_cfg: dict = {
            "source": "custom",
            "command": {
                "path": "npx",
                "args": [
                    "-y", "mcp-remote",
                    REMOTE_URL,
                    "--header", f"Authorization:Bearer {secret_key}",
                ],
            },
        }
    else:
        server_cfg = {
            "source": "custom",
            "command": {
                "path": "neo-mcp",
                "args": [],
                "env": {"NEO_SECRET_KEY": secret_key},
            },
        }

    try:
        data = _read_json_file(path) if path.exists() else {}
        data.setdefault("context_servers", {})["neo"] = server_cfg
        _write_json_file(path, data, no_backup)
        mode = "remote (via mcp-remote proxy)" if use_remote else "local"
        return True, f"Written to {path} ({mode})"
    except OSError as e:
        print(f"\n  Could not write {path}: {e}. Paste this manually:")
        print(json.dumps({"context_servers": {"neo": server_cfg}}, indent=2))
        return False, "Write failed — printed config to stdout"


def _configure_vscode(secret_key: str, opts: dict) -> tuple:
    use_remote = opts.get("remote", False)

    path = Path.cwd() / ".vscode" / "mcp.json"
    no_backup = opts.get("no_backup", False)

    if use_remote:
        server_cfg: dict = {
            "type": "http",
            "url": REMOTE_URL,
            "headers": {
                "Authorization": f"Bearer {secret_key}",
            },
        }
    else:
        server_cfg = {
            "type": "stdio",
            "command": "neo-mcp",
            "env": {"NEO_SECRET_KEY": secret_key},
        }

    try:
        data = _read_json_file(path) if path.exists() else {}
        data.setdefault("servers", {})["neo"] = server_cfg
        _write_json_file(path, data, no_backup)
        mode = "remote" if use_remote else "local"
        return True, f"Written to {path} ({mode})"
    except OSError as e:
        print(f"\n  Could not write {path}: {e}. Paste this manually:")
        print(json.dumps({"servers": {"neo": server_cfg}}, indent=2))
        return False, "Write failed — printed config to stdout"


def _configure_continue(secret_key: str, opts: dict) -> tuple:
    path = Path.home() / ".continue" / "config.json"
    no_backup = opts.get("no_backup", False)

    server_cfg = {
        "name": "neo",
        "transport": {
            "type": "stdio",
            "command": "neo-mcp",
            "env": {"NEO_SECRET_KEY": secret_key},
        },
    }

    try:
        data = _read_json_file(path) if path.exists() else {}
        servers = data.setdefault("mcpServers", [])
        data["mcpServers"] = [s for s in servers if s.get("name") != "neo"]
        data["mcpServers"].append(server_cfg)
        _write_json_file(path, data, no_backup)
        return True, f"Written to {path} (local stdio)"
    except OSError as e:
        print(f"\n  Could not write {path}: {e}. Paste this manually:")
        print(json.dumps({"mcpServers": [server_cfg]}, indent=2))
        return False, "Write failed — printed config to stdout"


def _configure_codex(secret_key: str, opts: dict) -> tuple:
    path = Path.home() / ".codex" / "config.json"
    no_backup = opts.get("no_backup", False)

    server_cfg = {
        "command": "neo-mcp",
        "env": {"NEO_SECRET_KEY": secret_key},
    }

    try:
        _merge_mcp_servers(path, "neo", server_cfg, no_backup)
        return True, f"Written to {path} (local stdio)"
    except OSError as e:
        print(f"\n  Could not write {path}: {e}. Paste this manually:")
        print(json.dumps({"mcpServers": {"neo": server_cfg}}, indent=2))
        return False, "Write failed — printed config to stdout"


_CONFIGURATORS = {
    "claude": _configure_claude,
    "cursor": _configure_cursor,
    "windsurf": _configure_windsurf,
    "zed": _configure_zed,
    "vscode": _configure_vscode,
    "continue": _configure_continue,
    "codex": _configure_codex,
}


def run_setup(args: list) -> None:
    """Entry point for `neo-mcp setup [flags]`.

    Flags:
      --secret-key KEY       Neo secret key (sk-v1-...)
      --editor EDITORS       Comma-separated: claude,cursor,windsurf,zed,vscode,continue,codex
      --remote               Use hosted mcp.heyneo.so instead of local stdio
      --scope SCOPE          Claude Code scope: user|project|local (default: user)
      --no-backup            Skip .bak file creation when overwriting configs
    """
    opts = _parse_args(args)
    is_tty = sys.stdin.isatty()

    print("Neo MCP Setup Wizard")
    print("=" * 40)

    # Get secret key
    secret_key = opts.get("secret_key") or os.environ.get("NEO_SECRET_KEY", "")

    if not secret_key:
        if is_tty:
            secret_key = getpass.getpass("Neo Secret Key (sk-v1-...): ").strip()
        else:
            print("Error: --secret-key required in non-interactive mode", file=sys.stderr)
            sys.exit(1)

    if not secret_key:
        print("Error: secret key is required.", file=sys.stderr)
        sys.exit(1)

    # Select editors
    if opts.get("editor"):
        selected = [e.strip().lower() for e in opts["editor"].split(",")]
    elif is_tty:
        selected = _prompt_editors()
    else:
        print("Error: --editor required in non-interactive mode", file=sys.stderr)
        sys.exit(1)

    valid_keys = {k for k, _ in EDITORS}
    invalid = [e for e in selected if e not in valid_keys]
    if invalid:
        print(f"Unknown editor(s): {', '.join(invalid)}", file=sys.stderr)
        print(f"Valid options: {', '.join(sorted(valid_keys))}", file=sys.stderr)
        sys.exit(1)

    # Ask once whether to use the remote hosted server (only if any selected editor supports it)
    if is_tty and not opts.get("remote") and any(e in _SUPPORTS_REMOTE for e in selected):
        opts["remote"] = _ask_remote()

    # Configure each editor
    print()
    results = []
    for editor_key in selected:
        label = dict(EDITORS)[editor_key]
        print(f"Configuring {label}...")
        ok, msg = _CONFIGURATORS[editor_key](secret_key, opts)
        results.append((label, ok, msg))

    # Summary
    print("\n" + "=" * 40)
    print("Setup Summary")
    print("=" * 40)
    for label, ok, msg in results:
        icon = "OK" if ok else "FAILED"
        print(f"  [{icon}] {label}: {msg}")

    success_count = sum(1 for _, ok, _ in results if ok)
    print(f"\n{success_count}/{len(results)} editor(s) configured.")
    if success_count > 0:
        print("Restart your editor(s) to activate the Neo MCP tools.")
        print("Verify: type /mcp (Claude Code) or check MCP settings.")
