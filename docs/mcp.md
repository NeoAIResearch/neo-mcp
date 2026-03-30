# Neo MCP — How It All Works

This document explains in full detail how Neo MCP works, what happens under the hood for each setup path, what the VS Code extension auto-configuration is, and what backend changes are needed to make every path seamless.

---

## The Core Problem Neo Solves

When a user asks Claude to "train a fraud detection model on fraud.csv", Claude cannot run Python locally — it has no execution environment. Neo provides that execution environment. The MCP server is the bridge: it gives Claude tools (`neo_submit_task`, `neo_task_status`, etc.) that send the work to Neo's backend, which routes it to a real execution environment (local daemon or cloud container), and brings results back.

---

## The Three Pieces

Every Neo MCP setup involves three pieces:

```
┌──────────────────┐       ┌──────────────────────┐       ┌─────────────────────┐
│   AI Editor      │  MCP  │   MCP Server         │  API  │   Neo Backend        │
│  (Claude Code,   │──────▶│  (translates MCP     │──────▶│  (routes tasks,      │
│   Cursor, etc.)  │       │   calls to Neo API)  │       │   manages threads)   │
└──────────────────┘       └──────────────────────┘       └──────────┬──────────┘
                                                                      │
                                                                      │ /v2/poll
                                                                      ▼
                                                           ┌─────────────────────┐
                                                           │   Daemon / Executor  │
                                                           │  (runs Python, bash, │
                                                           │   writes files)      │
                                                           └─────────────────────┘
```

The **MCP Server** can be either:
- **Hosted**: `https://mcpserver.heyneo.com/mcp` — runs on Neo's servers, stateless bridge
- **Local**: `neo-mcp` process on the user's machine (pip install), talks to Neo backend directly

The **Daemon / Executor** is always on the user's machine (or Neo's cloud). It:
- Polls `/v2/poll/{deployment_id}` continuously to receive commands from the backend
- Executes those commands: run Python scripts, write files, run bash
- Sends results back to the backend via `POST /v2/poll/response`
- Writes output files directly to the user's filesystem

**Without a running daemon, tasks hang.** The backend queues execution commands but nobody picks them up.

---

## The Deployment ID — The Routing Key

Every task submission includes a `deployment_id` — a UUID that tells Neo's backend which daemon to route execution commands to. The daemon registers itself under this UUID by polling `/v2/poll/{deployment_id}`. When a task is submitted with that UUID, the backend delivers commands to exactly that daemon.

```
Daemon polls:  GET /v2/poll/b989acd6-...  ←── backend knows this daemon is alive
Submit task:   POST /v2/thread/init-chat-direct { deployment_id: "b989acd6-..." }
Backend:       queues commands under b989acd6 → daemon picks them up → executes
```

If the `deployment_id` in the submission doesn't match any polling daemon, the backend returns an error: `"Failed to connect to sandbox"`.

### How deployment IDs are generated

There are two strategies:

**1. Extension UUID** — the VS Code/Cursor extension generates a random UUID on first install and persists it. Example: `b989acd6-ba17-43ee-a8d6-15b7e7f05a40`. Stable per-device.

**2. Key-derived UUID** — deterministic from the API key:
```python
UUID = SHA-256(NEO_SECRET_KEY)[:16]  →  formatted as UUID v5
```
Example: `sk-v1-a63a...` → always `de9d7297-580c-587c-b0e4-7ebb0fe7314c`.

Same key always produces same UUID. This is what the pip daemon and hosted MCP server both use — they independently derive the same UUID without any coordination.

---

## How `/v2/poll` Works

`/v2/poll/{deployment_id}` is a **long-poll endpoint**. The daemon calls it with a `wait_time=5` parameter — the backend holds the connection open for up to 5 seconds waiting for commands. If a task is submitted during that window, the backend delivers the command immediately. If nothing arrives, it returns an empty response and the daemon polls again.

```
Daemon:   GET /v2/poll/de9d7297?wait_time=5
Backend:  [holds connection open...]
          [task submitted → has commands]
          → returns: [{action: "run_python", script: "import pandas..."}]

Daemon:   executes the Python script
Daemon:   POST /v2/poll/response { request_id: ..., status: "success", stdout: "..." }
Backend:  updates thread, marks step complete

Daemon:   GET /v2/poll/de9d7297?wait_time=5   ← immediately polls again
```

The daemon authenticates with `NEO_SECRET_KEY` as a Bearer token — the same API key used for all other Neo API requests.

---

## Path 1: pip install — Local stdio

### What the user runs

```bash
pip install neo-mcp
claude mcp add --scope user neo \
  -e NEO_SECRET_KEY=sk-v1-... \
  -- neo-mcp
```

### What happens

```
claude mcp add writes to ~/.claude/claude_mcp_config.json:
{
  "neo": {
    "command": "neo-mcp",
    "env": { "NEO_SECRET_KEY": "sk-v1-..." }
  }
}

User opens Claude Code (new session)
Claude Code spawns: neo-mcp (subprocess, stdio)
server.py starts in stdio mode, NEO_SECRET_KEY is set in env

User: "train a classifier on data.csv"
Claude: calls neo_submit_task("train a classifier on data.csv")

server.py — neo_submit_task():
  1. _get_deployment_id()
       checks: X-Neo-Deployment-Id header → none (stdio mode)
       checks: NEO_DEPLOYMENT_ID env var → not set
       checks: _discover_sandbox_id() → reads ~/.neo/daemon/daemon.log
                                        reads ~/.neo/daemon/standalone_deployment_id
                                        → probably finds old UUID or nothing
       falls back: SHA-256(sk-v1-...)[:16] → de9d7297

  2. _python_daemon_running(de9d7297)?
       checks: ~/.neo/daemon/daemon_de9d7297.pid exists and process is alive → NO

  3. _register_with_daemon(de9d7297, sk-v1-...)
       tries: POST http://127.0.0.1:31337/register
       → 31337 is VS Code extension daemon port
       → if extension running: extension starts polling de9d7297 ← WORKS
       → if extension not running: connection refused → returns False

  4. _auto_start_daemon(sk-v1-..., de9d7297)
       spawns: neo-mcp daemon de9d7297
       waits up to 5s for daemon to write PID file

  neo-mcp daemon starts:
       reads NEO_SECRET_KEY from env
       derives same UUID: SHA-256(sk-v1-...)[:16] → de9d7297
       tries to poll: GET /v2/poll/de9d7297
                      Authorization: Bearer sk-v1-...
       TODAY:         → 401 Unauthorized ❌
       AFTER CHANGE:  → 200 OK ✅

  5. POST /v2/thread/init-chat-direct
       { deployment_id: "de9d7297", deployment_type: "vscode", message: "train..." }

  TODAY (daemon can't poll):
       backend: "Failed to connect to sandbox" ❌

  AFTER BACKEND CHANGE (daemon polling successfully):
       backend: { thread_id: "1bc15e3f-..." } ✅

       backend queues: run_python, write_file commands under de9d7297
       daemon picks them up via /v2/poll/de9d7297
       daemon runs: python train.py
       daemon writes: model.pkl → /home/user/project/model.pkl  ← LOCAL DISK ✅
       daemon sends results back via POST /v2/poll/response

       thread_id polling (API key works fine):
       GET /v2/thread/status/1bc15e3f → RUNNING → COMPLETED
       GET /v2/thread/thread-messages → full output
```

### Current auth

`GET /v2/poll/{deployment_id}` and `POST /v2/poll/response` accept `NEO_SECRET_KEY` as a Bearer token — the same API key used for all other Neo endpoints. No OAuth required.

### User experience

```bash
# npx (primary — no install)
claude mcp add --scope user neo --transport http https://mcpserver.heyneo.com/mcp --header "Authorization: Bearer sk-v1-..."

# pip (alternative)
pip install neo-mcp
claude mcp add --scope user neo -e NEO_SECRET_KEY=sk-v1-... -- neo-mcp
# Done. Daemon auto-starts on first task. No login, no browser, no UUID.
```

Files are written to the user's actual filesystem. Works on SSH servers, headless environments, CI/CD.

---

## Path 2: Hosted endpoint — HTTP transport

### What the user runs

```bash
# Option A: with local daemon for file execution
pip install neo-mcp
NEO_SECRET_KEY=sk-v1-... neo-mcp daemon &
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."

# Option B: no pip, no local daemon (cloud execution only)
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."
```

### What happens — Option A (with daemon)

```
claude mcp add writes config. User opens Claude Code.
Claude Code: HTTP request to https://mcpserver.heyneo.com/mcp

neo-mcp daemon running locally:
    derived UUID: SHA-256(sk-v1-...)[:16] → de9d7297
    polling: GET /v2/poll/de9d7297
             Authorization: Bearer sk-v1-...
    TODAY:   → 401 ❌
    AFTER CHANGE: → 200 ✅

User: "train a classifier on data.csv"
Claude: calls neo_submit_task via hosted MCP server

Hosted server (mcpserver.heyneo.com) receives:
    Authorization: Bearer sk-v1-...
    (no X-Neo-Deployment-Id header)

Hosted server:
    _get_deployment_id():
        no header → no env var → can't read user's localhost files
        falls back: SHA-256(sk-v1-...)[:16] → de9d7297
        ← SAME UUID the daemon derived from the same key ✅

    POST /v2/thread/init-chat-direct
    { deployment_id: "de9d7297", deployment_type: "vscode" }

    AFTER BACKEND CHANGE:
    backend routes commands to daemon polling de9d7297
    daemon on user's machine executes code
    files written to user's local disk ✅

    Status/messages tracking:
    GET /v2/thread/status → works with API key ✅
    GET /v2/thread/thread-messages → works with API key ✅
```

### What happens — Option B (no daemon, cloud execution)

```
Hosted server receives request with just API key.
Derives de9d7297 but nobody is polling it.

TODAY: POST /v2/thread/init-chat-direct { deployment_id: de9d7297 }
       → "Failed to connect to sandbox" ❌

AFTER BACKEND CHANGE 1 (cloud fallback):
POST /v2/thread/init-chat-direct { deployment_id: de9d7297 }
       → backend checks: is any daemon polling de9d7297? NO
       → backend: spin up cloud container, run task there
       → { thread_id: "..." } ✅

Task runs in cloud container.
Files stored in Neo S3 cloud storage.

neo_get_files:
    GET /v2/thread/{thread_id}/files → list of files with presigned S3 URLs
    downloads each file via S3 presigned URL
    returns file CONTENTS inline in the conversation

User gets:
    ### model.py
    ```python
    import sklearn
    ...
    ```

    ### requirements.txt
    ```
    scikit-learn==1.3.0
    ```

Claude can write these files to disk itself (it has file write tools).
```

### The file problem with cloud execution

For **generated code files** — Python scripts, configs, reports — inline content works fine. Claude receives the text, writes it to disk.

For **ML artifacts** — `model.pkl`, trained weights, large datasets — cloud mode fails:
- Binary files can't be returned as text
- Files over ~20k tokens hit the cap
- A 500MB trained model is simply unusable via chat

**Cloud mode is useful for: code generation, data analysis scripts, lightweight outputs.**
**Cloud mode fails for: actual model training, large file outputs, binary artifacts.**

For real ML work (the core Neo use case), the daemon must run locally.

---

## Path 3: VS Code/Cursor Extension

### What the user does today

1. Install Neo extension from marketplace
2. Log in through extension UI (OAuth browser flow)
3. Copy UUID from `~/.neo/daemon/standalone_deployment_id`
4. Run:
```bash
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..." \
  --header "X-Neo-Deployment-Id: b989acd6-..."
```

### What happens under the hood

```
Extension installed → user logs in → OAuth token saved to ~/.neo/daemon/mcp_auth.json

Extension starts PollerDaemon.js (TypeScript, runs at localhost:31337):
    registers deployment: b989acd6 (extension's own UUID)
    polls: GET /v2/poll/b989acd6
           Authorization: Bearer eyJhbG...  ← OAuth token
           → 200 OK ✅ (OAuth accepted)

User runs claude mcp add with X-Neo-Deployment-Id: b989acd6

Claude Code → hosted MCP server:
    Authorization: Bearer sk-v1-...
    X-Neo-Deployment-Id: b989acd6

Hosted server:
    _get_deployment_id():
        reads X-Neo-Deployment-Id header → b989acd6 ✅
        stores in session: session[session_id] = b989acd6

    neo_submit_task:
        deployment_id = b989acd6
        POST /v2/thread/init-chat-direct { deployment_id: "b989acd6" }
        → extension daemon is polling b989acd6 with OAuth → task routes ✅
        → extension executes code on user's machine
        → files written to user's local disk ✅
```

### What extension auto-configuration means

Today users must manually find their UUID and paste it into `claude mcp add`. This is terrible UX.

**Auto-configuration** = the extension does this automatically during login.

When the user logs into the extension, the extension already has:
- `user_api_key`: user just entered it
- `extension_uuid`: `b989acd6` — generated on first install, stored in extension state

The extension runs this during activation:

```typescript
import { exec } from 'child_process';

// Check if claude CLI is installed
exec('which claude', (err) => {
  if (err) return; // claude not installed, skip

  // Auto-configure MCP with correct UUID — user never has to touch terminal
  exec(
    `claude mcp add --scope user neo ` +
    `--transport http https://mcpserver.heyneo.com/mcp ` +
    `--header "Authorization: Bearer ${userApiKey}" ` +
    `--header "X-Neo-Deployment-Id: ${extensionUuid}"`,
    (err) => {
      if (!err) {
        vscode.window.showInformationMessage(
          'Neo MCP configured for Claude Code. Restart your Claude session.'
        );
      }
    }
  );
});
```

**User experience after this change:**

```
1. Install Neo extension from VS Code marketplace
2. Click "Log in" in extension sidebar, enter API key
3. Extension popup: "Neo MCP configured for Claude Code ✅"
4. Open new Claude Code session
5. Neo tools are available
```

Zero terminal commands. Zero UUID copying. Same UX as Cursor's built-in integrations.

**This requires no backend changes.** Just ~15 lines added to the extension's login handler.

---

## Summary — What Each Path Needs

### Path 1: pip install (local stdio)

```bash
pip install neo-mcp
claude mcp add --scope user neo -e NEO_SECRET_KEY=sk-v1-... -- neo-mcp
```

| Component | Status |
|---|---|
| UUID derivation (server + daemon use same key → same UUID) | ✅ Built |
| Daemon auto-start on first task | ✅ Built |
| Daemon sends API key to `/v2/poll` | ✅ Built |
| Backend accepts API key on `/v2/poll` | ❌ **Needs backend change** |
| Files written to local disk | ✅ Once daemon can poll |

**One backend change away from working perfectly.**

---

### Path 2: Hosted endpoint + local daemon

```bash
pip install neo-mcp
NEO_SECRET_KEY=sk-v1-... neo-mcp daemon &
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."
```

| Component | Status |
|---|---|
| Hosted server derives UUID from API key | ✅ Built |
| Daemon derives same UUID from same API key | ✅ Built |
| Daemon sends API key to `/v2/poll` | ✅ Built |
| Backend accepts API key on `/v2/poll` | ❌ **Needs backend change** |
| Files written to local disk | ✅ Once daemon can poll |

**Same backend change as Path 1. 3 commands total, could be reduced to 2 with `neo-mcp setup`.**

---

### Path 3: VS Code/Cursor extension

**Today:** 4 steps including manual UUID copy — bad UX.

**After extension auto-configure:** install + login = done. Zero terminal.

| Component | Status |
|---|---|
| Extension polls its UUID with OAuth | ✅ Works today |
| Extension has user API key + its UUID | ✅ Available at login time |
| Extension calls `claude mcp add` on login | ❌ **Needs ~15 lines in extension** |
| Files written to local disk | ✅ Works today |

**No backend changes needed. Extension code change only.**

---

### Path 4: No pip, no extension (cloud)

```bash
claude mcp add --scope user neo \
  --transport http https://mcpserver.heyneo.com/mcp \
  --header "Authorization: Bearer sk-v1-..."
```

| Component | Status |
|---|---|
| One command setup | ✅ |
| Cloud execution when no daemon | ❌ **Needs backend change (cloud fallback)** |
| Code file contents returned inline | ✅ `neo_get_files` fetches from S3 |
| Binary ML artifacts (model.pkl, weights) | ❌ Not viable via chat |
| Large files (datasets, trained models) | ❌ Not viable via chat |

**Good for code generation. Not good for actual model training with file outputs.**

---

## Recommended Priority

### 1. Extension: auto-configure on login (~15 lines)

Extension calls `claude mcp add` with its UUID when the user logs in. Zero terminal commands for VS Code/Cursor users.

Result: install extension → log in → Neo tools appear. No UUID, no terminal.

### 3. Backend: cloud fallback (lowest priority for ML use case)

When no daemon is polling the deployment_id, fall back to cloud execution. Best for lightweight code generation. Not suitable as primary path for ML workflows with file outputs.

---

## Why Files Matter

The core Neo use case is training models and running ML pipelines. The outputs are:
- `model.pkl` — trained scikit-learn / XGBoost model (binary, 1MB–500MB)
- `model.pt` — PyTorch weights (binary, 100MB–10GB)
- `training_results.csv` — evaluation metrics
- `features.parquet` — processed dataset

These **cannot** be delivered via chat message. They must be written to the user's filesystem. This is why the daemon model exists — the daemon runs on the user's machine, writes files directly to their project directory.

Cloud execution (`neo_get_files`) works for:
- Generated Python scripts (returned as text)
- Small reports (HTML, markdown)
- Config files

Cloud execution does **not** work for:
- Any binary file
- Any file over ~1MB
- Files that need to be at specific paths for downstream tools

**For Neo's primary use case, the daemon must run locally. Cloud is a secondary convenience.**
