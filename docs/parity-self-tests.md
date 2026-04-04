# Neo MCP Parity Self-Test Playbook

This playbook is for manual validation that `python` (`pip`) and `npm` packages behave like the VS Code extension daemon model, especially for deployment routing and workspace isolation.

## Prerequisites

- Valid `NEO_SECRET_KEY`
- Two test workspaces on your machine:
  - `/tmp/neo-test-a`
  - `/tmp/neo-test-b`
- Optional second machine (or VM) for cross-machine collision tests
- Installed CLIs:
  - `neo-mcp` (pip package)
  - `neo-mcp-daemon` (npm package, via `npx`)

## Global Rules for Every Test

- Record:
  - daemon start command
  - deployment ID used
  - workspace path
  - created files and final paths
- A test **fails** if any file is written outside the intended workspace.

---

## 1) Cross-Machine Isolation (Default Mode)

Goal: same API key on two machines must not mix file writes.

1. On machine A:
   - `NEO_SECRET_KEY=... neo-mcp daemon`
2. On machine B:
   - `NEO_SECRET_KEY=... neo-mcp daemon`
3. Submit separate tasks from each machine that create unique marker files.
4. Verify:
   - Machine A marker exists only on A.
   - Machine B marker exists only on B.

Expected: no cross-machine file creation.

---

## 2) Intentional Collision Check (Key-Derived Override)

Goal: prove deterministic mode is collision-prone if reused across machines.

1. On both machines:
   - `export NEO_DEPLOYMENT_ID_MODE=key-derived`
   - `NEO_SECRET_KEY=... neo-mcp daemon`
2. Submit tasks from both machines concurrently writing same filename in different local paths.
3. Observe routing behavior.

Expected: collision risk is observable in this mode.  
Recovery: unset `NEO_DEPLOYMENT_ID_MODE` and restart daemon(s).

---

## 3) Explicit Deployment ID Override

Goal: verify explicit override takes precedence.

1. Start daemon:
   - `NEO_SECRET_KEY=... NEO_DEPLOYMENT_ID=11111111-2222-3333-4444-555555555555 neo-mcp daemon`
2. Submit task and inspect daemon logs / task routing.

Expected: daemon always uses provided explicit deployment ID.

---

## 4) Workspace Isolation Under Concurrency

Goal: high-parallel writes must stay per-thread/per-workspace.

1. Create 20 tasks in parallel:
   - 10 targeting `/tmp/neo-test-a`
   - 10 targeting `/tmp/neo-test-b`
2. Each task writes:
   - same filename (for stress), and
   - unique content marker.
3. Verify resulting files.

Expected:
- files for A stay in A only
- files for B stay in B only
- no mixed content markers.

---

## 5) Hosted HTTP Routing Header Validation

Goal: remote MCP config must include `X-Neo-Deployment-Id` and route locally.

1. Configure remote MCP via setup.
2. Inspect generated config (`~/.cursor/mcp.json`, Claude config, or `.vscode/mcp.json`).
3. Confirm headers include:
   - `Authorization: Bearer ...`
   - `X-Neo-Deployment-Id: <uuid>`
4. Submit a file-creating task through hosted MCP.

Expected: task executes on your local daemon workspace, not elsewhere.

---

## 6) Daemon Crash/Restart Resilience

Goal: restart during active execution should not reroute writes.

1. Start a long-running task with multiple file writes.
2. Kill daemon mid-run (`kill -9 <pid>`).
3. Restart daemon with same environment.
4. Let task continue or resubmit continuation step.

Expected: subsequent writes remain in intended workspace; no fallback to wrong folder.

---

## 7) Symlink Escape Safety

Goal: ensure file operations remain policy-safe.

1. Inside workspace:
   - `ln -s /etc /tmp/neo-test-a/outside-link`
2. Submit task attempting to write/read via symlinked escape path.

Expected: operation is blocked or safely remapped; no unauthorized external write.

---

## 8) Path Normalization Edge Cases

Goal: platform-specific path quirks do not break routing.

Run these on applicable OS:

1. Windows:
   - mixed separators (`\` + `/`)
   - long paths
   - reserved names (`CON`, `NUL`)
2. macOS:
   - Unicode normalized filename pairs (NFC vs NFD)
3. Linux:
   - deep relative traversal attempts (`../../..`)

Expected: safe behavior with no escapes and correct final file placement.

---

## 9) Rapid Control Transitions

Goal: pause/resume/stop transitions do not corrupt thread mapping.

1. Submit active task.
2. Run rapid sequence:
   - pause
   - resume
   - pause
   - stop
3. Re-check:
   - thread status
   - workspace mapping file
   - new task submission after cleanup

Expected: no stale routing state; new tasks route correctly.

---

## 10) npm vs pip Behavior Spot-Check

Goal: both packages show equivalent local execution semantics.

1. Run test set 4, 6, and 7 once with:
   - `neo-mcp daemon` (pip)
2. Repeat with:
   - `npx --yes neo-mcp-daemon /tmp/neo-test-a` (npm)

Expected: equivalent outcomes on routing, isolation, and safety.

---

## Result Template

Use this per test:

```text
Test ID:
Package: pip | npm
OS:
Deployment mode: machine-default | key-derived | explicit
Expected:
Actual:
Pass/Fail:
Notes:
```
