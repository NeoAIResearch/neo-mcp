#!/usr/bin/env bash
# start-daemon.sh — Standalone Neo daemon launcher (no VS Code extension required)
#
# Usage:
#   export NEO_SECRET_KEY=sk-v1-...
#   export NEO_API_KEY=ak-v1-...
#   bash scripts/start-daemon.sh [/path/to/workspace]
#
# The script compiles the daemon TypeScript, starts PollerDaemon.js in the
# background, registers a deployment, and sends periodic heartbeats so the
# daemon stays alive.  The Neo MCP server picks up the deployment ID
# automatically via _discover_sandbox_id() — no extra config needed.

set -euo pipefail

WORKSPACE="${1:-$(pwd)}"
DAEMON_PORT=31337
DAEMON_DIR="$HOME/.neo/daemon"
UUID_FILE="$DAEMON_DIR/standalone_deployment_id"
PID_FILE="$DAEMON_DIR/standalone.pid"
HEARTBEAT_PID_FILE="$DAEMON_DIR/standalone_heartbeat.pid"

# ---------------------------------------------------------------------------
# 1. Check prerequisites
# ---------------------------------------------------------------------------
if ! command -v node &>/dev/null; then
  echo "ERROR: Node.js is not installed. Install it from https://nodejs.org" >&2
  exit 1
fi
if ! command -v npm &>/dev/null; then
  echo "ERROR: npm is not installed. Install Node.js from https://nodejs.org" >&2
  exit 1
fi
if [ -z "${NEO_SECRET_KEY:-}" ]; then
  echo "ERROR: NEO_SECRET_KEY is not set. Export it before running this script." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Locate the extension directory (works regardless of CWD)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXT_DIR="$REPO_ROOT/vscode_extension/neo-vscode-extension"
DAEMON_JS="$EXT_DIR/out/daemon/PollerDaemon.js"

if [ ! -d "$EXT_DIR" ]; then
  echo "ERROR: Extension directory not found: $EXT_DIR" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Compile daemon TypeScript (idempotent — skips if out/ is up to date)
# ---------------------------------------------------------------------------
TSCONFIG="$EXT_DIR/tsconfig.json"
NEEDS_COMPILE=false

if [ ! -f "$DAEMON_JS" ]; then
  NEEDS_COMPILE=true
else
  # Recompile if any source file is newer than the compiled output
  if find "$EXT_DIR/src" -name "*.ts" -newer "$DAEMON_JS" | grep -q .; then
    NEEDS_COMPILE=true
  fi
fi

if [ "$NEEDS_COMPILE" = true ]; then
  echo "Compiling daemon TypeScript..."
  (
    cd "$EXT_DIR"
    npm install --silent
    npm run compile
  )
  echo "Compilation complete."
else
  echo "Compiled output is up to date, skipping compilation."
fi

if [ ! -f "$DAEMON_JS" ]; then
  echo "ERROR: Compilation failed — $DAEMON_JS not found." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Generate or reuse a stable deployment UUID
# ---------------------------------------------------------------------------
mkdir -p "$DAEMON_DIR"
if [ -f "$UUID_FILE" ]; then
  DEPLOYMENT_ID="$(cat "$UUID_FILE")"
else
  DEPLOYMENT_ID="$(python3 -c "import uuid; print(uuid.uuid4())")"
  echo "$DEPLOYMENT_ID" > "$UUID_FILE"
fi

# ---------------------------------------------------------------------------
# 5. Kill any stale standalone daemon
# ---------------------------------------------------------------------------
if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping existing daemon (PID: $OLD_PID)..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

# Kill any stale heartbeat sender
if [ -f "$HEARTBEAT_PID_FILE" ]; then
  OLD_HB_PID="$(cat "$HEARTBEAT_PID_FILE")"
  kill "$OLD_HB_PID" 2>/dev/null || true
  rm -f "$HEARTBEAT_PID_FILE"
fi

# Also check if something is already bound to the port (e.g. VS Code extension)
if curl -sf "http://127.0.0.1:$DAEMON_PORT/health" &>/dev/null; then
  echo "A daemon is already running on port $DAEMON_PORT."
  echo "Registering standalone deployment with the existing daemon..."
  DAEMON_ALREADY_RUNNING=true
else
  DAEMON_ALREADY_RUNNING=false
fi

# ---------------------------------------------------------------------------
# 6. Start the daemon (detached) if not already running
# ---------------------------------------------------------------------------
if [ "$DAEMON_ALREADY_RUNNING" = false ]; then
  NEO_BACKEND_URL="${NEO_API_URL:-https://master.heyneo.so}" \
    node "$DAEMON_JS" \
    >> "$DAEMON_DIR/standalone_stdout.log" 2>&1 &
  DAEMON_PID=$!
  echo "$DAEMON_PID" > "$PID_FILE"
  echo "Daemon started (PID: $DAEMON_PID)"
fi

# ---------------------------------------------------------------------------
# 7. Wait for daemon to be healthy (up to 10 × 0.5 s = 5 s)
# ---------------------------------------------------------------------------
HEALTHY=false
for i in $(seq 1 10); do
  if curl -sf "http://127.0.0.1:$DAEMON_PORT/health" &>/dev/null; then
    HEALTHY=true
    break
  fi
  sleep 0.5
done

if [ "$HEALTHY" = false ]; then
  echo "ERROR: Daemon did not become healthy within 5 seconds." >&2
  if [ -f "$PID_FILE" ]; then
    echo "Check logs: $DAEMON_DIR/standalone_stdout.log" >&2
  fi
  exit 1
fi

# ---------------------------------------------------------------------------
# 8. Read the daemon's local auth token (written by the daemon at startup)
# ---------------------------------------------------------------------------
TOKEN_FILE="$DAEMON_DIR/daemon.token"
# Wait up to 3 s for the token file to appear (daemon writes it on first start)
for i in $(seq 1 6); do
  [ -f "$TOKEN_FILE" ] && break
  sleep 0.5
done

if [ ! -f "$TOKEN_FILE" ]; then
  echo "ERROR: Daemon token file not found at $TOKEN_FILE" >&2
  exit 1
fi

DAEMON_TOKEN="$(cat "$TOKEN_FILE")"

# ---------------------------------------------------------------------------
# 9. Register the deployment with the daemon
# ---------------------------------------------------------------------------
REGISTER_RESPONSE="$(curl -sf -X POST "http://127.0.0.1:$DAEMON_PORT/register" \
  -H "Authorization: Bearer $DAEMON_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"deploymentId\":\"$DEPLOYMENT_ID\",\"workspaceFolder\":\"$WORKSPACE\",\"authToken\":\"$NEO_SECRET_KEY\"}")"

if [ -z "$REGISTER_RESPONSE" ]; then
  echo "ERROR: Failed to register deployment with daemon." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 10. Write sandboxId entry so _discover_sandbox_id() picks it up
# ---------------------------------------------------------------------------
echo "{\"sandboxId\": \"$DEPLOYMENT_ID\", \"source\": \"standalone\"}" >> "$DAEMON_DIR/daemon.log"

# ---------------------------------------------------------------------------
# 11. Start a background heartbeat sender to keep deployment alive
#     (daemon evicts deployments with no heartbeat for > 5 minutes)
# ---------------------------------------------------------------------------
(
  while true; do
    sleep 60
    curl -sf -X POST "http://127.0.0.1:$DAEMON_PORT/heartbeat" \
      -H "Authorization: Bearer $DAEMON_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"deploymentId\":\"$DEPLOYMENT_ID\"}" &>/dev/null || true
  done
) &
HB_PID=$!
echo "$HB_PID" > "$HEARTBEAT_PID_FILE"

# ---------------------------------------------------------------------------
# 12. Print confirmation
# ---------------------------------------------------------------------------
DAEMON_PID_DISPLAY="${DAEMON_PID:-$(cat "$PID_FILE" 2>/dev/null || echo "existing")}"
echo ""
echo "Neo daemon running (PID: $DAEMON_PID_DISPLAY)"
echo "Deployment ID: $DEPLOYMENT_ID"
echo "Workspace:     $WORKSPACE"
echo "Heartbeat:     running in background (PID: $HB_PID)"
echo ""
echo "MCP will auto-discover this daemon. No extra config needed."
echo "To force-pin it: add -e NEO_DEPLOYMENT_ID=$DEPLOYMENT_ID to your claude mcp add command."
echo ""
echo "To stop:  kill \$(cat $PID_FILE) \$(cat $HEARTBEAT_PID_FILE)"
echo "Logs:     $DAEMON_DIR/daemon.log"
