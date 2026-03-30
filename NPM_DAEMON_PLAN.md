# NPM Daemon Package Plan

Replace the pip-based daemon with a Node.js npm package so users need no Python installation.

---

## Goal

**Before:**
```bash
pip install neo-mcp
neo-mcp daemon &
```


**After:**
```bash
npx neo-mcp-daemon &
```

Agent runs this automatically on first task. User clicks yes once. Done.

---

## Package

- **Name:** `neo-mcp-daemon`
- **Published to:** npmjs.com
- **Runtime:** Node.js (pre-installed on most dev machines)
- **Command:** `npx neo-mcp-daemon` — no install step, npx fetches and runs on demand

---

## What the daemon does (port from Python)

1. **Derive deployment UUID** from `NEO_SECRET_KEY` — `SHA-256(key)[:16]` formatted as UUID (same algorithm as Python daemon, same UUID)
2. **Poll the backend** — `GET <poll-endpoint>/<deployment_id>` with `Authorization: Bearer <key>` (once backend accepts API keys)
3. **Execute commands** received from backend — write files, run scripts in the workspace directory
4. **Send responses** back — `POST <poll-response-endpoint>` with execution results
5. **Persist deployment ID** — write to `~/.neo/daemon/standalone_deployment_id` (same path as Python daemon for compatibility)
6. **Write PID file** — `~/.neo/daemon/daemon.pid` so the MCP server can detect it's running
7. **Heartbeat** — keepalive every 60s to prevent backend eviction

---

## File structure

```
npm/
├── package.json
├── tsconfig.json
├── src/
│   ├── index.ts          # entry point — parses args, starts daemon
│   ├── daemon.ts         # main poll loop
│   ├── executor.ts       # executes commands (write files, run scripts)
│   ├── auth.ts           # derives deployment UUID from API key
│   └── paths.ts          # ~/.neo/daemon/* path constants (shared with Python paths)
├── bin/
│   └── neo-mcp-daemon    # CLI entry point (referenced in package.json bin)
└── dist/                 # compiled JS (published to npm)
```

---

## package.json key fields

```json
{
  "name": "neo-mcp-daemon",
  "version": "1.0.0",
  "bin": {
    "neo-mcp-daemon": "./bin/neo-mcp-daemon"
  },
  "engines": { "node": ">=18" }
}
```

---

## Auth

Same key-derived UUID as the Python daemon:

```typescript
import { createHash } from 'crypto';

function deriveDeploymentId(secretKey: string): string {
  const hash = createHash('sha256').update(secretKey).digest();
  const hex = hash.slice(0, 16).toString('hex');
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join('-');
}
```

Same UUID as the hosted MCP server and Python daemon — routing works automatically, no headers needed.

The daemon authenticates with `NEO_SECRET_KEY` directly — no OAuth, no login step.

---

## MCP server changes needed

In `server.py` — update `handle_error(400)` and the daemon auto-start logic to prefer `npx neo-mcp-daemon` over `neo-mcp daemon`:

```python
# Priority order when starting daemon:
# 1. npx neo-mcp-daemon   (Node.js — no install needed)
# 2. neo-mcp daemon       (Python pip — fallback if Node not available)
```

The `DAEMON_NOT_RUNNING` message becomes:
```
Please run this command to start the Neo daemon:
  npx neo-mcp-daemon &
```

---

## Compatibility

- Writes to the same `~/.neo/daemon/` paths as the Python daemon
- Same deployment UUID algorithm — Python and Node daemons are interchangeable
- VS Code extension auto-detects either daemon via `standalone_deployment_id`

---

## Publishing to npm

### Prerequisites

```bash
# One-time: create npm account at npmjs.com and login
npm login

# Verify you're logged in as the right user/org
npm whoami
```

### Build and publish

```bash
cd npm

# Install deps
npm install

# Compile TypeScript → dist/
npm run build

# Run tests before publishing
npm test

# Dry run — see what gets published (check files list)
npm pack --dry-run

# Publish (first time — public package)
npm publish --access public

# Subsequent releases — bump version first
npm version patch   # or minor / major
npm publish
```

### Version strategy

- Patch `1.0.x` — bug fixes, no protocol changes
- Minor `1.x.0` — new action handlers, compatible with existing backend
- Major `x.0.0` — breaking protocol changes (coordinate with backend team)

### CI/CD (GitHub Actions)

Add `.github/workflows/publish-npm.yml`:

```yaml
name: Publish npm daemon
on:
  push:
    tags: ['npm-v*']
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          registry-url: 'https://registry.npmjs.org'
      - run: cd npm && npm ci && npm run build && npm test
      - run: cd npm && npm publish --access public
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
```

Store `NPM_TOKEN` (automation token from npmjs.com) in GitHub repo secrets.

### Testing before publish

```bash
# Test locally with npm link (no publish needed)
cd npm && npm link
neo-mcp-daemon /path/to/workspace &

# Test via npx from local tarball
npm pack
npx ./neo-mcp-daemon-1.0.0.tgz /path/to/workspace &

# Test published package (after publish)
npx neo-mcp-daemon /path/to/workspace &
```

### Verify the package works

```bash
# Start daemon with API key
export NEO_SECRET_KEY=sk-v1-...
npx neo-mcp-daemon /tmp/test-workspace &

# Expected output:
# Neo npm daemon ready
#   deployment_id : <uuid>
#   workspace     : /tmp/test-workspace
#   backend       : https://master.heyneo.so
#   pid           : <pid>
# Polling for commands...

# Check PID file was written
cat ~/.neo/daemon/npm_daemon.pid

# Check sandbox log was written (for MCP server discovery)
cat ~/.neo/daemon/daemon.log
```

---

## Rollout

1. Build and publish `neo-mcp-daemon` to npm (see Publishing section above)
2. Update all docs and skills — replace `pip install neo-mcp` + `neo-mcp daemon` with `npx neo-mcp-daemon`
3. The Python daemon remains fully independent — users choose one or the other
4. `server.py` handle_error(400) already mentions `npx neo-mcp-daemon` as the recommended command

---

## Final user experience

```
User:  "Train a fraud detection model on fraud.csv"

Agent: Calls neo_submit_task
       ← DAEMON_NOT_RUNNING

Agent: "Neo needs a local daemon to execute tasks. Can I start it?"
       [Yes]

Agent: runs: npx neo-mcp-daemon &
       (Node.js fetches and starts daemon automatically)

Agent: Retries neo_submit_task → task submitted → execution begins
```

Zero pip. Zero Python. One yes-click.
