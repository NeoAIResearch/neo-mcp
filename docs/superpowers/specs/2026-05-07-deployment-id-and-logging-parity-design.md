# Deployment-ID Sharing & Logging Parity with VS Code Extension

**Date:** 2026-05-07
**Status:** Approved (autonomous mode)
**Scope:** `python/`, `npm/` only. The VS Code extension is **read-only reference** — its code and runtime behavior must not change.

## Problem

Today the three daemons (pip MCP server, npm daemon, VS Code extension) each compute a deployment ID independently and log to `~/.neo/daemon/` in different formats:

| | Deployment ID storage | Runtime log file | Log format | Rotation |
|---|---|---|---|---|
| Extension | VS Code globalState, keyed `MD5(userId + machineId + remoteName)` | `~/.neo/daemon/daemon.log` (when `NEO_LOGS_DUMP=true`) | `[ts] [LEVEL] msg {meta}` | 12 h × 4 archives |
| pip | `~/.neo/daemon/standalone_deployment_id` (file) | `~/.neo/daemon/neo-mcp.log` | `%(asctime)s %(levelname)s %(name)s: %(message)s` | none |
| npm | same file | none — stderr only | n/a | n/a |

Three concrete issues:

1. **Different deployment IDs** for the same machine when both extension and pip/npm are installed → backend treats them as separate sandboxes; tasks may run in unexpected workspaces.
2. **Python's `neo-mcp.log` never rotates** → unbounded growth.
3. **npm has no runtime log file** → after the parent shell closes, debug info is gone.

## Goals

1. **Single deployment ID per machine**, shared by all three. File is canonical.
2. **`neo-mcp.log` becomes the pip/npm runtime log file** with the same line format and rotation policy as the extension's `Logger.ts`.
3. `daemon.log` keeps its current role as the **shared startup ledger** (one entry per daemon start, format already aligned in the previous change).

## Non-goals

- Per-user deployment-ID sharding.
- Switching to a structured logging library.
- Changing the extension's runtime log destination (extension keeps writing runtime logs to its own `daemon.log` per existing code paths; we are not touching that).
- Changing wire-protocol field names (backend still expects `sandbox_id`).

## Design

### 1. Shared deployment ID

`~/.neo/daemon/standalone_deployment_id` becomes the canonical source for **all three** daemons.

#### pip — `python/src/neo_mcp/auth.py`

`get_or_create_deployment_id(secret_key)` precedence (unchanged): `NEO_DEPLOYMENT_ID` env > `NEO_DEPLOYMENT_ID_MODE=key-derived` > shared file > generate.

Replace the existing `if exists: read; else: generate, write_text` with an **atomic create-only** path to eliminate the TOCTOU race when multiple processes start simultaneously:

```python
fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
# write candidate, close
# on FileExistsError → re-read and use whatever is on disk
```

Read-only filesystem still degrades silently to in-memory UUID (preserves current safety net).

#### npm — `npm/src/auth.ts`

Move `getOrCreateDeploymentId` from `daemon.ts` into `auth.ts` (keep re-export from `daemon.ts` for back-compat with tests). Same atomic-write change using `fs.openSync(path, 'wx', 0o600)` (POSIX `O_EXCL` semantics in Node).

#### Extension — **untouched**

The VS Code extension is the reference implementation we mirror; we do not modify it. Its `StateManager.getDeploymentId()` continues to scope by `(userId, machineId, remoteName)` in `globalState`, generating a fresh UUIDv4 on first use.

**Implication:** the extension and pip/npm operate in two **independent** ID spaces. On a machine that runs both, the extension uses one UUID and pip/npm use another. The backend treats them as separate sandboxes. This is the deliberate trade-off for keeping the extension immutable. If end-to-end ID alignment is needed later, the change should be made in the broker the extension exposes (`PollerDaemon` HTTP API at `127.0.0.1:31337`) — pip/npm could opt into reading from that broker. Out of scope here.

### 2. Logging parity

#### Shared line format (matches `Logger.ts:131-134`)

```
[2026-05-07T10:31:22.253Z] [INFO] message text {"deploymentId":"...","logger":"backend_poller"}
```

Mandatory metadata: `deploymentId` (auto-injected by the logger constructor). Additional context (logger name, exception, etc.) merges in.

#### Rotation rules (match `Logger.ts:18-19`)

- Rotate when the current log file is **≥ 12 h old** (age, not size).
- Keep **4 rotated files** (`.1` through `.4`); oldest is deleted on rotate.
- Hourly rotation check (timer is `unref()`'d in npm; daemon thread in Python is daemon-thread).

#### Birth-time portability

The extension uses `fs.statSync(path).birthtimeMs`. On Linux Node this can be 0 on filesystems without statx birth-time support, which would cause perpetual rotation. We add a **sidecar file** `<logfile>.birth` containing the Unix epoch timestamp written when the log is first created (and rewritten on every rotation). Both Python and npm read this file to compute age. If the sidecar is missing for a pre-existing log (upgrade case), backfill with the file's `mtime` as a best-effort starting point.

#### Python — new file `python/src/neo_mcp/log_utils.py`

```python
class RotatingDaemonHandler(logging.Handler):
    def __init__(self, log_file: Path, deployment_id: str): ...
    def emit(self, record): ...   # check age, rotate if needed, write line

def setup_daemon_logging(deployment_id: str, log_file: Path) -> None:
    # Replace root logger handlers with RotatingDaemonHandler
```

`server.py:_setup_logging` is reordered: deployment ID is resolved **first**, then `setup_daemon_logging(deployment_id, ~/.neo/daemon/neo-mcp.log)` installs the handler. Format is `[ISO] [LEVEL] {logger_name}: {msg} {meta}` — i.e. `record.name` lands inside the meta JSON to keep the human-readable prefix consistent with the extension.

`backend_poller.py:_write_daemon_log` keeps writing the **single startup line** to `daemon.log` directly (no logger involvement) — that file is intentionally a separate ledger, not the runtime log.

#### npm — new file `npm/src/logger.ts`

```typescript
export class DaemonLogger {
  constructor(logFile: string, deploymentId: string, suppressConsole?: boolean) { ... }
  info(msg: string, meta?: object): void { ... }
  warn(msg: string, meta?: object): void { ... }
  error(msg: string, meta?: object): void { ... }
  debug(msg: string, meta?: object): void { ... }
  close(): void { ... }
}
```

Writes to file always (file is the canonical destination for the daemon). Console mirroring goes to `console.error` (stderr) so MCP stdio mode never corrupts stdout. Honors `NEO_SUPPRESS_CONSOLE` like the extension.

`daemon.ts` constructs one `DaemonLogger` after `depId` resolution and replaces every `console.error(...)` call with `logger.info/warn/error(...)`. Same in `mcp-server.ts`. The single early `console.error` for "NEO_SECRET_KEY missing" stays raw because the logger needs the deploymentId, which we don't have if NEO_SECRET_KEY is missing — printing to stderr is the correct fail-fast behavior.

`daemon.ts:writeSandboxLog` keeps writing the startup ledger entry to `daemon.log` (separate file, separate concern — no logger involvement).

### 3. File layout after the change

```
~/.neo/daemon/
├── standalone_deployment_id        canonical UUID (shared)
├── daemon.log                       startup ledger (one line per daemon start)
├── neo-mcp.log                      pip/npm runtime log (rotated, 12h × 4)
├── neo-mcp.log.1 … .4               rotated archives
├── neo-mcp.log.birth                sidecar — birth timestamp for rotation
├── npm_daemon.pid                   (existing)
├── neo-mcp.lock / neo-mcp.pid       (existing)
└── thread-workspaces.json           (existing)
```

## Testing

### Python — `python/tests/test_system.py`

- `TestRotatingDaemonHandler`:
  - format: `[ISO] [LEVEL] msg {meta}` is regex-matched
  - `deploymentId` is present in every meta
  - rotation triggers when `<logfile>.birth` is set to >12 h ago
  - after rotation, `.1` exists and current file is fresh
  - `.4` is dropped when `.3` exists and a 5th rotation occurs
- `TestSharedDeploymentIdAtomic`:
  - 5 concurrent `get_or_create_deployment_id()` calls return the same UUID
  - file ends up containing exactly one valid UUID

### npm — `npm/tests/system.test.ts`

- `describe('DaemonLogger')`: format, deploymentId injection, rotation on aged birth file, `.4` cap.
- `describe('shared deployment id atomic')`: same concurrency check via `Promise.all`.

### Extension

No tests, no edits — the extension is untouched.

## Verification

```bash
# Python
python3 -m pytest python/tests/test_system.py -v -k "Rotating or DeploymentIdAtomic"

# npm
cd npm && npx vitest run tests/system.test.ts -t "DaemonLogger"

# Manual smoke
rm -rf /tmp/neo-smoke && NEO_HOME=/tmp/neo-smoke python3 -c "
from neo_mcp.auth import get_or_create_deployment_id
print(get_or_create_deployment_id('sk-v1-test'))
"
ls /tmp/neo-smoke/daemon/
cat /tmp/neo-smoke/daemon/standalone_deployment_id

# Drive logging — start the server briefly
NEO_HOME=/tmp/neo-smoke NEO_SECRET_KEY=sk-v1-test timeout 3 neo-mcp || true
head /tmp/neo-smoke/daemon/neo-mcp.log
```

Expected: `neo-mcp.log` contains lines matching `^\[20\d\d-.*Z\] \[(INFO|WARN|ERROR)\] .* \{.*"deploymentId".*\}$`.

## Out of scope (deferred)

- Per-user deployment-ID sharding (`standalone_deployment_id.<userhash>`).
- A `NEO_LOGS_DUMP=false` gate to disable file logging entirely (extension has it; we always log).
- Migrating the extension's *runtime* log to `neo-mcp.log` so all three share one runtime file.
