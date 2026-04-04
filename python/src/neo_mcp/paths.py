"""Filesystem paths under ~/.neo/daemon/ (mirrors PollerDaemon.ts path constants)."""

from pathlib import Path

NEO_DIR: Path = Path.home() / ".neo"
DAEMON_DIR: Path = NEO_DIR / "daemon"

# Lock file — prevents duplicate poller instances
LOCK_FILE: Path = DAEMON_DIR / "neo-mcp.lock"

# PID file — written on startup so other processes can check if we're alive
PID_FILE: Path = DAEMON_DIR / "neo-mcp.pid"

# Append-only log of daemon starts: {"sandboxId": "...", "source": "neo-mcp"}
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
