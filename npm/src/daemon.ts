/**
 * Neo npm daemon — simple poll-execute loop.
 *
 * Polls the Neo backend for commands, executes them locally, sends results back.
 * Thread → workspace mapping persisted in ~/.neo/daemon/thread-workspaces.json.
 */

import { randomUUID } from 'crypto';
import {
  appendFileSync, closeSync, existsSync, linkSync, mkdirSync, mkdtempSync, openSync,
  readFileSync, renameSync, statSync, unlinkSync, writeFileSync, writeSync,
} from 'fs';
import { resolve } from 'path';
import { deriveDeploymentId, getAuthToken } from './auth.js';
import { NEO_API_URL, POLL_MAX_INTERVAL, POLL_MAX_MESSAGES } from './config.js';
import { Command, dispatch, setExecutorLogger } from './executor.js';
import { DaemonLogger } from './logger.js';
import { getTaskStatus } from './neo-client.js';
import {
  DAEMON_DIR, DAEMON_LOG, NEO_MCP_LOG, NPM_PID_FILE, STANDALONE_UUID_FILE,
  STATUSES_FILE, WORKSPACES_FILE, deploymentReadyFile, pidFileForDeployment,
} from './paths.js';

const NPM_VERSION = String(
  (JSON.parse(readFileSync(resolve(__dirname, '..', 'package.json'), 'utf8')) as { version?: string })
    .version ?? 'unknown',
);

// Module-level logger — created lazily so getOrCreateDeploymentId() can run
// even when nothing is going to be logged (e.g. setThreadStatus from tests).
let _logger: DaemonLogger | null = null;
function getLogger(deploymentId: string): DaemonLogger {
  if (!_logger) _logger = new DaemonLogger(NEO_MCP_LOG, deploymentId);
  return _logger;
}

// Read at call time so tests can override via env var after module load.
function getMaxThreadWorkspaces(): number { return Number(process.env['NEO_THREAD_WORKSPACES_MAX'] ?? 500); }
function getThreadWorkspacesTtlMs(): number { return Number(process.env['NEO_THREAD_WORKSPACES_TTL_SECONDS'] ?? 7 * 24 * 60 * 60) * 1000; }

// ---------------------------------------------------------------------------
// Command concurrency semaphore — mirrors Python BackendPoller._cmd_semaphore
// Limits parallel command handlers so a large poll batch doesn't spawn
// unbounded tasks or overwhelm the local filesystem.
// ---------------------------------------------------------------------------

const _MAX_CONCURRENT_COMMANDS = 32;

class _Semaphore {
  private _count: number;
  private readonly _queue: Array<() => void> = [];
  constructor(count: number) { this._count = count; }
  async acquire(): Promise<void> {
    if (this._count > 0) { this._count--; return; }
    await new Promise<void>(resolve => this._queue.push(resolve));
  }
  release(): void {
    if (this._queue.length > 0) { this._queue.shift()!(); }
    else { this._count++; }
  }
}

const _cmdSemaphore = new _Semaphore(_MAX_CONCURRENT_COMMANDS);

// ---------------------------------------------------------------------------
// Thread status gate — mirrors Python BackendPoller._thread_statuses
// ---------------------------------------------------------------------------

const _ALWAYS_ACCEPTED = new Set(['RUNNING']);
const _DRAIN_ACCEPTED = new Set(['RUNNING', 'PAUSED']);
const _threadStatuses = new Map<string, string>();
let _statusMtime: number | undefined;
let _draining = false;
let _drainRemaining = 0;

function parkTickMs(): number {
  return Number(process.env['NEO_POLL_PARK_TICK_MS'] ?? 2000);
}
function drainEmptyPolls(): number {
  return Number(process.env['NEO_POLL_DRAIN_EMPTY'] ?? 3);
}
function emptyStreakBeforeStatus(): number {
  return Number(process.env['NEO_POLL_STATUS_CHECK_AFTER'] ?? 3);
}

export function loadThreadStatuses(): Record<string, string> {
  try {
    const raw = JSON.parse(readFileSync(STATUSES_FILE, 'utf8')) as Record<string, unknown>;
    const out: Record<string, string> = {};
    for (const [tid, val] of Object.entries(raw)) {
      if (typeof val === 'string' && val) out[tid] = val;
      else if (val && typeof val === 'object' && typeof (val as { status?: unknown }).status === 'string') {
        out[tid] = (val as { status: string }).status;
      }
    }
    return out;
  } catch {
    return {};
  }
}

function statusesMtime(): number | undefined {
  try {
    return statSync(STATUSES_FILE).mtimeMs;
  } catch {
    return undefined;
  }
}

export function writeThreadStatus(threadId: string, status: string): void {
  if (!threadId || !status) return;
  let data: Record<string, { status: string; updated_at: number }> = {};
  try {
    const raw = JSON.parse(readFileSync(STATUSES_FILE, 'utf8')) as Record<string, unknown>;
    for (const [tid, val] of Object.entries(raw)) {
      if (typeof val === 'string' && val) {
        data[tid] = { status: val, updated_at: 0 };
      } else if (val && typeof val === 'object' && typeof (val as { status?: unknown }).status === 'string') {
        const updated = (val as { updated_at?: unknown }).updated_at;
        data[tid] = {
          status: (val as { status: string }).status,
          updated_at: typeof updated === 'number' ? updated : 0,
        };
      }
    }
  } catch {
    data = {};
  }
  data[threadId] = { status, updated_at: Math.floor(Date.now() / 1000) };
  mkdirSync(DAEMON_DIR, { recursive: true });
  const tmpFile = `${STATUSES_FILE}.tmp-${process.pid}`;
  writeFileSync(tmpFile, JSON.stringify(data, null, 2));
  renameSync(tmpFile, STATUSES_FILE);
}

function deploymentInUse(): boolean {
  for (const status of _threadStatuses.values()) {
    if (status === 'RUNNING') return true;
  }
  return false;
}

function beginDrain(): void {
  _draining = true;
  _drainRemaining = drainEmptyPolls();
}

function reloadStatusesIfChanged(): void {
  const mtime = statusesMtime();
  if (mtime === _statusMtime) return;
  const wasInUse = deploymentInUse();
  _threadStatuses.clear();
  for (const [tid, status] of Object.entries(loadThreadStatuses())) {
    _threadStatuses.set(tid, status);
  }
  _statusMtime = mtime;
  if (deploymentInUse()) {
    _draining = false;
    _drainRemaining = 0;
  } else if (wasInUse) {
    beginDrain();
  }
}

/**
 * Record a thread's lifecycle status so the command gate can allow/reject it.
 * Persists to thread-statuses.json so a detached daemon (or a later restart)
 * sees pause/stop/resume written by the MCP tools.
 */
export function setThreadStatus(threadId: string, status: string): void {
  const wasInUse = deploymentInUse();
  _threadStatuses.set(threadId, status);
  writeThreadStatus(threadId, status);
  _statusMtime = statusesMtime();
  if (deploymentInUse()) {
    _draining = false;
    _drainRemaining = 0;
  } else if (wasInUse) {
    beginDrain();
  }
}

/** Test helper — clear in-memory park/drain state between cases. */
export function resetPollerStateForTests(): void {
  _threadStatuses.clear();
  _statusMtime = undefined;
  _draining = false;
  _drainRemaining = 0;
}

/** Returns false when the thread is known to be terminated/failed — new commands should be rejected. */
function shouldAccept(threadId: string): boolean {
  const status = _threadStatuses.get(threadId);
  if (status === undefined) return true; // unknown → allow (backwards compat, mirrors Python)
  if (_ALWAYS_ACCEPTED.has(status)) return true;
  if (_draining && _DRAIN_ACCEPTED.has(status)) return true;
  return false;
}

/**
 * Append a startup entry to ~/.neo/daemon/daemon.log.
 *
 * Format matches the VS Code extension's DaemonLogger (Logger.ts):
 *   [<ISO timestamp>] [INFO] <message> <meta json>
 *
 * Both ``deploymentId`` and ``sandboxId`` are emitted — the former is
 * canonical, the latter kept for back-compat with older readers.
 */
function writeSandboxLog(deploymentId: string): void {
  mkdirSync(DAEMON_DIR, { recursive: true });
  const ts = new Date().toISOString();
  const meta = JSON.stringify({ deploymentId, sandboxId: deploymentId, source: 'npm-daemon' });
  appendFileSync(DAEMON_LOG, `[${ts}] [INFO] npm-daemon started ${meta}\n`);
}

function identityPayload(deploymentId: string): Record<string, unknown> {
  return {
    pid: process.pid,
    impl: 'npm',
    version: NPM_VERSION,
    started_at: new Date().toISOString(),
    deployment_id: deploymentId,
  };
}

function writePidFiles(deploymentId: string): void {
  mkdirSync(DAEMON_DIR, { recursive: true });
  const body = JSON.stringify(identityPayload(deploymentId)) + '\n';
  writeFileSync(NPM_PID_FILE, body);
  writeFileSync(pidFileForDeployment(deploymentId), body);
}

function writeReadiness(deploymentId: string): void {
  reloadStatusesIfChanged();
  const submissionIntents = [..._threadStatuses.entries()]
    .filter(([tid, status]) => tid.startsWith('__submission__:') && status === 'RUNNING')
    .map(([tid]) => tid)
    .sort();
  const payload = {
    deployment_id: deploymentId,
    pid: process.pid,
    impl: 'npm',
    version: NPM_VERSION,
    observed_at: Date.now() / 1000,
    submission_intents: submissionIntents,
  };
  mkdirSync(DAEMON_DIR, { recursive: true });
  const readyPath = deploymentReadyFile(deploymentId);
  const tmpFile = `${readyPath}.tmp-${process.pid}`;
  writeFileSync(tmpFile, JSON.stringify(payload));
  renameSync(tmpFile, readyPath);
}

function cleanupPidFiles(deploymentId: string): void {
  try { unlinkSync(NPM_PID_FILE); } catch { /* ignore */ }
  try { unlinkSync(pidFileForDeployment(deploymentId)); } catch { /* ignore */ }
  try { unlinkSync(deploymentReadyFile(deploymentId)); } catch { /* ignore */ }
}

export function getOrCreateDeploymentId(): string {
  if (process.env['NEO_DEPLOYMENT_ID']) return process.env['NEO_DEPLOYMENT_ID'];
  const mode = (process.env['NEO_DEPLOYMENT_ID_MODE'] ?? '').trim().toLowerCase();
  const token = getAuthToken();
  if ((mode === 'key-derived' || mode === 'key' || mode === 'deterministic') && token) {
    return deriveDeploymentId(token);
  }

  mkdirSync(DAEMON_DIR, { recursive: true });

  // Always prefer a persisted machine-specific UUID over key-derived.
  // This prevents command-queue collision when the same API key is used on
  // multiple machines simultaneously — each machine must have its own sandbox.
  // Mirrors what the VS Code extension does (unique UUID per user+machine).
  if (existsSync(STANDALONE_UUID_FILE)) {
    const uid = readFileSync(STANDALONE_UUID_FILE, 'utf8').trim();
    if (uid) return uid;
  }

  // No persisted UUID yet — concurrency-safe via temp-file + linkSync.
  // The candidate is written into a temp file with content already in place,
  // then linked atomically. linkSync fails if the target exists, so a loser
  // re-reads the winner's content. This avoids the window where a plain
  // O_EXCL create would let other processes observe an empty file before
  // the winner finishes writing.
  const candidate = randomUUID();
  let tmpPath: string | null = null;
  try {
    const tmpDir = mkdtempSync(`${STANDALONE_UUID_FILE}-tmp-`);
    tmpPath = `${tmpDir}/uuid`;
    writeFileSync(tmpPath, candidate, { mode: 0o600 });
    try {
      linkSync(tmpPath, STANDALONE_UUID_FILE);
      return candidate;
    } catch (e: unknown) {
      if ((e as NodeJS.ErrnoException)?.code === 'EEXIST') {
        // Loser of the race — read the winner's value.
        try {
          const existing = readFileSync(STANDALONE_UUID_FILE, 'utf8').trim();
          if (existing) return existing;
        } catch { /* fall through to candidate */ }
      }
      return candidate;
    } finally {
      try { unlinkSync(tmpPath); } catch { /* ignore */ }
      try { require('fs').rmdirSync(tmpDir); } catch { /* ignore */ }
    }
  } catch {
    return candidate;
  }
}

/**
 * Load thread→workspace mappings with timestamps from the persisted file.
 * Mirrors Python _list_tasks() which reads raw JSON to get updated_at for sorting.
 */
export function loadThreadWorkspacesWithMeta(): Record<string, { workspace: string; updated_at: string | number }> {
  try {
    const data = JSON.parse(readFileSync(WORKSPACES_FILE, 'utf8')) as Record<string, unknown>;
    const out: Record<string, { workspace: string; updated_at: string | number }> = {};
    for (const [tid, value] of Object.entries(data)) {
      if (!value || typeof value !== 'object') continue;
      const workspace = (value as { workspace?: unknown }).workspace;
      const updated_at = (value as { updated_at?: unknown }).updated_at;
      if (typeof workspace !== 'string') continue;
      const ts = typeof updated_at === 'string' || typeof updated_at === 'number' ? updated_at : '';
      out[tid] = { workspace, updated_at: ts };
    }
    return out;
  } catch {
    return {};
  }
}

export function loadThreadWorkspaces(): Record<string, string> {
  try {
    const data = JSON.parse(readFileSync(WORKSPACES_FILE, 'utf8')) as Record<string, unknown>;
    const out: Record<string, string> = {};
    for (const [tid, value] of Object.entries(data)) {
      if (typeof value === 'string') out[tid] = value;
      else if (value && typeof value === 'object' && typeof (value as { workspace?: unknown }).workspace === 'string') {
        out[tid] = (value as { workspace: string }).workspace;
      }
    }
    return out;
  } catch {
    return {};
  }
}

// Module-level workspace cache shared by registerThreadWorkspace() and runDaemon().
// Eliminates the concurrent-registration race: two simultaneous task submissions
// used to both read an empty file, then overwrite each other — losing one entry.
// Now registration mutates this dict directly; no file read-modify-write needed.
const _sharedThreadWorkspaces: Record<string, string> = {};
let _sharedWorkspacesLoaded = false;

function _ensureSharedWorkspacesLoaded(): void {
  if (_sharedWorkspacesLoaded) return;
  _sharedWorkspacesLoaded = true;
  Object.assign(_sharedThreadWorkspaces, loadThreadWorkspaces());
}

export function registerThreadWorkspace(threadId: string, workspace: string): void {
  _ensureSharedWorkspacesLoaded();
  _sharedThreadWorkspaces[threadId] = workspace;
  saveThreadWorkspaces(_sharedThreadWorkspaces);
  if (_logger) _logger.info('registerThreadWorkspace', { threadId, workspace });
}

export function saveThreadWorkspaces(workspaces: Record<string, string>): void {
  const now = Date.now();
  const minTs = now - getThreadWorkspacesTtlMs();
  let previous: Record<string, { workspace: string; updated_at: number }> = {};
  try {
    const raw = JSON.parse(readFileSync(WORKSPACES_FILE, 'utf8')) as Record<string, unknown>;
    for (const [tid, value] of Object.entries(raw)) {
      if (!value || typeof value !== 'object') continue;
      const workspace = (value as { workspace?: unknown }).workspace;
      const updated = (value as { updated_at?: unknown }).updated_at;
      // Accept Unix-seconds numbers (npm format) or ISO8601 strings (Python format)
      let updatedMs: number | undefined;
      if (typeof updated === 'number') {
        updatedMs = updated * 1000;
      } else if (typeof updated === 'string' && updated.length > 0) {
        const parsed = Date.parse(updated);
        if (!isNaN(parsed)) updatedMs = parsed;
      }
      if (typeof workspace === 'string' && updatedMs !== undefined) {
        previous[tid] = { workspace, updated_at: updatedMs / 1000 };
      }
    }
  } catch {
    previous = {};
  }
  let entries = Object.entries(workspaces).map(([tid, workspace]) => {
    const prev = previous[tid];
    const prevTs = prev && prev.workspace === workspace ? prev.updated_at * 1000 : now;
    return { tid, workspace, updatedAt: prevTs };
  });
  entries = entries.filter((e) => e.updatedAt >= minTs && !!e.workspace);
  const maxWorkspaces = getMaxThreadWorkspaces();
  if (entries.length > maxWorkspaces) {
    entries = entries.slice(entries.length - maxWorkspaces);
  }
  mkdirSync(DAEMON_DIR, { recursive: true });
  const payload = Object.fromEntries(
    entries.map((e) => [e.tid, { workspace: e.workspace, updated_at: Math.floor(e.updatedAt / 1000) }])
  );
  const tmpFile = `${WORKSPACES_FILE}.tmp-${process.pid}`;
  writeFileSync(tmpFile, JSON.stringify(payload, null, 2));
  renameSync(tmpFile, WORKSPACES_FILE);
}

async function fetchWithTimeout(url: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

/** Thrown by pollBackend when the API key is rejected — caller should stop the daemon. */
export class AuthError extends Error {
  constructor(message: string) { super(message); this.name = 'AuthError'; }
}

export async function pollBackend(depId: string, token: string, waitTime = 5): Promise<Command[]> {
  let res: Response;
  try {
    res = await fetchWithTimeout(
      `${NEO_API_URL}/v2/poll/${depId}?max_messages=${POLL_MAX_MESSAGES}&wait_time=${waitTime}`,
      { headers: { 'Authorization': `Bearer ${token}` } },
      Math.max(waitTime * 2, 10) * 1_000, // timeout = 2× wait_time, minimum 10s
    );
  } catch {
    return [];
  }
  if (res.status === 401) {
    // Throw so runDaemon can stop — mirrors Python BackendPoller UNAUTHORIZED handling.
    throw new AuthError(`Auth rejected for deployment ${depId} (401). Check NEO_SECRET_KEY.`);
  }
  if (res.status === 404 || !res.ok) return [];
  const data = await res.json() as Command[] | { messages?: Command[] };
  return Array.isArray(data) ? data : (data.messages ?? []);
}

// Retry sendResponse up to 3 times — silent failure was the root cause of stalled file creation.
// The backend only generates the next write_code command after receiving the previous response.
// If sendResponse silently failed, the backend would stall waiting for a confirmation that never arrived.
export async function sendResponse(depId: string, token: string, response: Record<string, unknown>): Promise<void> {
  const body = JSON.stringify({ ...response, sandbox_id: response['sandbox_id'] ?? depId });
  let delayMs = 500;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await fetchWithTimeout(`${NEO_API_URL}/v2/poll/response`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        body,
      }, 30_000);
      return; // success
    } catch (err) {
      if (attempt < 3) {
        await sleep(delayMs);
        delayMs *= 2; // 500ms → 1000ms
      } else {
        if (_logger) _logger.error('sendResponse failed after 3 attempts', { request_id: response['request_id'] ?? null, error: String(err) });
      }
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function runDaemon(opts: { workspace?: string; deploymentId?: string; signal?: AbortSignal } = {}): Promise<void> {
  const workspace = resolve(opts.workspace ?? process.cwd());
  const depId = opts.deploymentId ?? getOrCreateDeploymentId();
  const token = getAuthToken();
  if (!token) {
    console.error('ERROR: NEO_SECRET_KEY is not set.\nSet your API key: export NEO_SECRET_KEY=sk-v1-...');
    process.exit(1);
  }

  mkdirSync(DAEMON_DIR, { recursive: true });
  writePidFiles(depId);
  writeSandboxLog(depId);
  writeReadiness(depId);
  const readyTicker = setInterval(() => {
    try { writeReadiness(depId); } catch { /* ignore */ }
  }, 500);

  // Initialise the runtime logger now that we know deploymentId. Every line
  // it writes carries deploymentId in the meta automatically.
  const logger = getLogger(depId);
  setExecutorLogger(logger);
  logger.info('Neo daemon ready', {
    workspace,
    backend: NEO_API_URL,
    pid: process.pid,
  });

  // Use the module-level shared dict so registrations made by the MCP server
  // (in the same process) are immediately visible here without any file reload.
  _ensureSharedWorkspacesLoaded();
  const threadWorkspaces = _sharedThreadWorkspaces;
  let backoffMs = 1_000;
  let running = true;

  const stop = (): void => {
    running = false;
    clearInterval(readyTicker);
    cleanupPidFiles(depId);
  };
  process.on('SIGTERM', () => { stop(); process.exit(0); });
  process.on('SIGINT',  () => { stop(); process.exit(0); });
  opts.signal?.addEventListener('abort', stop, { once: true });

  let lastCommandTime = 0;       // Date.now() ms, 0 = never
  let emptyStreak = 0;
  reloadStatusesIfChanged();

  while (running) {
    reloadStatusesIfChanged();
    if (!deploymentInUse() && !_draining) {
      await sleep(parkTickMs());
      continue;
    }

    // During active execution use wait_time=1 so the poll returns quickly after the
    // backend queues the next command. wait_time=5 is fine when idle (reduces poll traffic).
    const recentlyActive = (Date.now() - lastCommandTime) < 60_000;
    const waitTime = recentlyActive ? 1 : 5;
    let commands: Command[];
    try {
      commands = await pollBackend(depId, token, waitTime);
    } catch (e) {
      if (e instanceof AuthError) {
        // Mirrors Python BackendPoller UNAUTHORIZED handling — stop the daemon.
        logger.error('Auth rejected — stopping daemon', { message: e.message });
        stop();
        return;
      }
      commands = [];
    }

    if (commands.length === 0) {
      emptyStreak++;
      if (_draining) {
        _drainRemaining--;
        if (_drainRemaining <= 0) {
          _draining = false;
          logger.info('Drain complete — parking v2/poll');
        }
      } else if (deploymentInUse() && emptyStreak >= emptyStreakBeforeStatus()) {
        emptyStreak = 0;
        const runningIds = [..._threadStatuses.entries()]
          .filter(([, s]) => s === 'RUNNING')
          .map(([tid]) => tid);
        for (const tid of runningIds) {
          try {
            const data = await getTaskStatus(token, tid);
            const newStatus = typeof data['status'] === 'string' ? data['status'] : '';
            if (newStatus && newStatus !== 'RUNNING') {
              setThreadStatus(tid, newStatus);
            }
          } catch {
            // Transient status errors must not park the loop.
          }
        }
      }
      if (recentlyActive || _draining) {
        // Small yield so the event loop can process signals/timers before next poll.
        await sleep(100);
      } else {
        await sleep(backoffMs);
        backoffMs = Math.min(Math.floor(backoffMs * 1.5), POLL_MAX_INTERVAL);
      }
      continue;
    }

    backoffMs = 1_000;
    lastCommandTime = Date.now();
    emptyStreak = 0;

    // Dispatch all commands in this batch concurrently — each runs in its own
    // thread's workspace so there is no ordering dependency between them.
    await Promise.all(commands.map(async (cmd) => {
      const tid = cmd.thread_id as string | undefined;

      // Resolve the local workspace for this thread.  The MCP server writes
      // thread→workspace to the shared file right after submit; the file may
      // not exist yet if this command raced the registration.  Retry with
      // back-off before falling back so all 10 concurrent projects each land
      // in their own directory instead of sharing the daemon's default.
      let ws: string | undefined = tid ? threadWorkspaces[tid] : undefined;
      if (tid && !ws) {
        // Attempt 1 — synchronous file reload (usually sufficient in stdio mode)
        Object.assign(threadWorkspaces, loadThreadWorkspaces());
        ws = threadWorkspaces[tid];
      }
      if (tid && !ws) {
        // Attempt 2 — wait 250 ms for MCP server to finish writing the file
        await sleep(250);
        Object.assign(threadWorkspaces, loadThreadWorkspaces());
        ws = threadWorkspaces[tid];
      }
      if (tid && !ws) {
        // Attempt 3 — one final reload at 500 ms
        await sleep(500);
        Object.assign(threadWorkspaces, loadThreadWorkspaces());
        ws = threadWorkspaces[tid];
      }
      if (tid && !ws) {
        // Still nothing — log a warning so the user can diagnose misrouted files.
        // Do NOT persist the fallback: if the registration arrives later we want
        // the next command to pick up the real workspace, not a cached default.
        logger.warn('workspace not registered for thread — using default', { tid, default: workspace });
      }
      const resolvedWs = ws ?? workspace;

      // Thread status gate — mirrors Python BackendPoller._should_accept().
      // Reject commands for TERMINATED/FAILED threads to avoid executing stale
      // commands that arrive after neo_stop_task was called.
      if (tid && !shouldAccept(tid)) {
        const currentStatus = _threadStatuses.get(tid) ?? 'UNKNOWN';
        logger.warn('Rejecting command for terminated thread', { tid, status: currentStatus });
        const errorResp: Record<string, unknown> = {
          request_id: cmd.request_id,
          status: 'error',
          error: `Thread is ${currentStatus} — not accepting commands`,
          thread_id: tid,
        };
        if (cmd.response_queue_name) errorResp['response_queue_name'] = cmd.response_queue_name;
        await sendResponse(depId, token, errorResp);
        return;
      }

      // Semaphore: limit concurrent handlers — mirrors Python _cmd_semaphore(32)
      logger.info('dispatching command', { action: cmd.action, tid: tid ?? null });
      await _cmdSemaphore.acquire();
      let result: Record<string, unknown>;
      try {
        result = await dispatch(cmd, resolvedWs) as unknown as Record<string, unknown>;
      } finally {
        _cmdSemaphore.release();
      }

      if (tid) {
        result['thread_id'] = tid;
      }
      if (cmd.response_queue_name) {
        result['response_queue_name'] = cmd.response_queue_name;
      }
      await sendResponse(depId, token, result);
    }));
  }
}
