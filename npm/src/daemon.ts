/**
 * Neo npm daemon — simple poll-execute loop.
 *
 * Polls the Neo backend for commands, executes them locally, sends results back.
 * Thread → workspace mapping persisted in ~/.neo/daemon/thread-workspaces.json.
 */

import { randomUUID } from 'crypto';
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'fs';
import { resolve } from 'path';
import { deriveDeploymentId, getAuthToken } from './auth.js';
import { NEO_API_URL } from './config.js';
import { Command, dispatch } from './executor.js';
import {
  DAEMON_DIR, DAEMON_LOG, NPM_PID_FILE, STANDALONE_UUID_FILE,
  WORKSPACES_FILE, pidFileForDeployment,
} from './paths.js';

const MAX_THREAD_WORKSPACES = Number(process.env['NEO_THREAD_WORKSPACES_MAX'] ?? 500);
const THREAD_WORKSPACES_TTL_MS = Number(process.env['NEO_THREAD_WORKSPACES_TTL_SECONDS'] ?? 7 * 24 * 60 * 60) * 1000;

function writeSandboxLog(deploymentId: string): void {
  mkdirSync(DAEMON_DIR, { recursive: true });
  appendFileSync(DAEMON_LOG, `${JSON.stringify({ sandboxId: deploymentId, source: 'npm-daemon' })}\n`);
}

function writePidFiles(deploymentId: string): void {
  mkdirSync(DAEMON_DIR, { recursive: true });
  writeFileSync(NPM_PID_FILE, String(process.pid));
  writeFileSync(pidFileForDeployment(deploymentId), String(process.pid));
}

function cleanupPidFiles(deploymentId: string): void {
  try { unlinkSync(NPM_PID_FILE); } catch { /* ignore */ }
  try { unlinkSync(pidFileForDeployment(deploymentId)); } catch { /* ignore */ }
}

export function getOrCreateDeploymentId(): string {
  if (process.env['NEO_DEPLOYMENT_ID']) return process.env['NEO_DEPLOYMENT_ID'];
  const sk = process.env['NEO_SECRET_KEY'];
  if (sk) return deriveDeploymentId(sk);

  mkdirSync(DAEMON_DIR, { recursive: true });
  if (existsSync(STANDALONE_UUID_FILE)) {
    const uid = readFileSync(STANDALONE_UUID_FILE, 'utf8').trim();
    if (uid) return uid;
  }
  const uid = randomUUID();
  writeFileSync(STANDALONE_UUID_FILE, uid);
  return uid;
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

export function registerThreadWorkspace(threadId: string, workspace: string): void {
  const existing = loadThreadWorkspaces();
  existing[threadId] = workspace;
  saveThreadWorkspaces(existing);
}

export function saveThreadWorkspaces(workspaces: Record<string, string>): void {
  const now = Date.now();
  const minTs = now - THREAD_WORKSPACES_TTL_MS;
  let previous: Record<string, { workspace: string; updated_at: number }> = {};
  try {
    const raw = JSON.parse(readFileSync(WORKSPACES_FILE, 'utf8')) as Record<string, unknown>;
    for (const [tid, value] of Object.entries(raw)) {
      if (!value || typeof value !== 'object') continue;
      const workspace = (value as { workspace?: unknown }).workspace;
      const updated = (value as { updated_at?: unknown }).updated_at;
      if (typeof workspace === 'string' && typeof updated === 'number') {
        previous[tid] = { workspace, updated_at: updated };
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
  if (entries.length > MAX_THREAD_WORKSPACES) {
    entries = entries.slice(entries.length - MAX_THREAD_WORKSPACES);
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

export async function pollBackend(depId: string, token: string, waitTime = 5): Promise<Command[]> {
  try {
    const res = await fetchWithTimeout(
      `${NEO_API_URL}/v2/poll/${depId}?max_messages=10&wait_time=${waitTime}`,
      { headers: { 'Authorization': `Bearer ${token}` } },
      Math.max(waitTime * 2, 10) * 1_000, // timeout = 2× wait_time, minimum 10s
    );
    if (res.status === 401) {
      console.error(`Auth rejected for deployment ${depId} (401).`);
      return [];
    }
    if (res.status === 404 || !res.ok) return [];
    const data = await res.json() as Command[] | { messages?: Command[] };
    return Array.isArray(data) ? data : (data.messages ?? []);
  } catch {
    return [];
  }
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
        console.error(`[sendResponse] Failed after 3 attempts for request_id=${response['request_id'] ?? '?'}:`, err);
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

  console.log('Neo daemon ready');
  console.log(`  deployment_id : ${depId}`);
  console.log(`  workspace     : ${workspace}`);
  console.log(`  backend       : ${NEO_API_URL}`);
  console.log(`  pid           : ${process.pid}`);

  const threadWorkspaces = loadThreadWorkspaces();
  let backoffMs = 1_000;
  let running = true;

  const stop = (): void => { running = false; cleanupPidFiles(depId); };
  process.on('SIGTERM', () => { stop(); process.exit(0); });
  process.on('SIGINT',  () => { stop(); process.exit(0); });
  opts.signal?.addEventListener('abort', stop, { once: true });

  let lastCommandTime = 0; // Date.now() ms, 0 = never

  while (running) {
    // During active execution use wait_time=1 so the poll returns quickly after the
    // backend queues the next command. wait_time=5 is fine when idle (reduces poll traffic).
    const recentlyActive = (Date.now() - lastCommandTime) < 60_000;
    const waitTime = recentlyActive ? 1 : 5;
    const commands = await pollBackend(depId, token, waitTime);

    if (commands.length === 0) {
      if (recentlyActive) {
        // Small yield so the event loop can process signals/timers before next poll.
        await sleep(100);
      } else {
        await sleep(backoffMs);
        backoffMs = Math.min(Math.floor(backoffMs * 1.5), 3_000);
      }
      continue;
    }

    backoffMs = 1_000;
    lastCommandTime = Date.now();

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
        console.warn(`[daemon] workspace not registered for thread ${tid} — using default: ${workspace}`);
      }
      const resolvedWs = ws ?? workspace;

      const result = await dispatch(cmd, resolvedWs) as unknown as Record<string, unknown>;

      if (tid) {
        result['thread_id'] = tid;
        // Only persist the mapping when it came from an explicit registration
        // (not the fallback default) so a late-arriving registration can still
        // override a previously saved default.
        if (ws && !threadWorkspaces[tid]) {
          threadWorkspaces[tid] = ws;
          saveThreadWorkspaces(threadWorkspaces);
        }
      }
      if (cmd.response_queue_name) {
        result['response_queue_name'] = cmd.response_queue_name;
      }
      await sendResponse(depId, token, result);
    }));
  }
}
