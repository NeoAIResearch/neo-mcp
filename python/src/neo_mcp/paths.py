"""Filesystem paths under ~/.neo/daemon/ (mirrors PollerDaemon.ts path constants).

Honors the NEO_HOME env var (same contract as npm/src/paths.ts) so tests can
redirect state to a tmp dir without touching the user's real ~/.neo.
"""

import os
from pathlib import Path

NEO_DIR: Path = Path(os.environ["NEO_HOME"]) if os.environ.get("NEO_HOME") else Path.home() / ".neo"
DAEMON_DIR: Path = NEO_DIR / "daemon"

# Optional user settings file. Schema: {"env": "staging" | "prod"}.
# When present, takes precedence over NEO_ENVIRONMENT/NEO_ENV/NEO_API_URL for
# selecting the backend base URL. See config.py:_resolve_api_url.
SETTINGS_FILE: Path = NEO_DIR / "settings.json"

# Lock file — prevents duplicate poller instances
LOCK_FILE: Path = DAEMON_DIR / "neo-mcp.lock"

# PID file — written on startup so other processes can check if we're alive
PID_FILE: Path = DAEMON_DIR / "neo-mcp.pid"

# Append-only log of daemon starts. Format mirrors the VS Code extension's
# DaemonLogger (Logger.ts) so both daemons produce parseable interleaved lines:
#   [<ISO timestamp>] [INFO] <message> {"deploymentId": "...", "sandboxId": "...", "source": "..."}
# `sandboxId` is duplicated for back-compat with older readers.
DAEMON_LOG: Path = DAEMON_DIR / "daemon.log"

# Machine-specific deployment UUID written by the npm daemon on first run.
# Mirrors npm/src/paths.ts STANDALONE_UUID_FILE.
STANDALONE_UUID_FILE: Path = DAEMON_DIR / "standalone_deployment_id"

# thread_id → workspace path mapping written by daemon, read by MCP tools
THREAD_WORKSPACES_FILE: Path = DAEMON_DIR / "thread-workspaces.json"

# In-progress and completed job metadata
JOBS_FILE: Path = DAEMON_DIR / "neo-mcp-jobs.json"

# Per-job log directory
JOBS_LOG_DIR: Path = DAEMON_DIR / "neo-mcp-logs"

# ---------------------------------------------------------------------------
# Third-party integrations (GitHub, HuggingFace, Anthropic, OpenRouter, ...)
# ---------------------------------------------------------------------------

# Shared contract with the VS Code extension: metadata only, no secrets.
INTEGRATIONS_METADATA_FILE: Path = NEO_DIR / "integrations.json"

# Secrets that have no native credential file (LLM API keys) land here as
# key=value .env files with mode 0o600.
INTEGRATIONS_DIR: Path = NEO_DIR / "integrations"
