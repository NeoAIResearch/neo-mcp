/**
 * Neo npm daemon — main poll loop.
 *
 * Polls GET /v2/poll/{deployment_id} for commands and dispatches them via executor.ts.
 * Sends results back via POST /v2/poll/response.
 *
 * Auth priority: OAuth token from mcp_auth.json → NEO_SECRET_KEY env var.
 * Same wire protocol as the Python daemon.
 */

import { appendFileSync, mkdirSync, writeFileSync } from 'fs';
import { homedir } from 'os';
import { resolve } from 'path';
import { deriveDeploymentId, getAuthToken, loadMcpAuth, refreshAuthToken, saveMcpAuth } from './auth.js';
import { NEO_API_URL } from './config.js';
import { Command, dispatch } from './executor.js';
import {
  DAEMON_DIR, DAEMON_LOG, NPM_PID_FILE, STANDALONE_UUID_FILE,
  WORKSPACES_FILE, pidFileForDeployment,
} from './paths.js';


// ---------------------------------------------------------------------------
// Deployment ID
// ---------------------------------------------------------------------------

function getOrCreateDeploymentId(): string {
  if (process.env['NEO_DEPLOYMENT_ID']) return process.env['NEO_DEPLOYMENT_ID'];
  const sk = process.env['NEO_SECRET_KEY'];
  if (sk) return deriveDeploymentId(sk);

  mkdirSync(DAEMON_DIR, { recursive: true });
  try {
    const { readFileSync } = require('fs') as typeof import('fs');
    const uid = readFileSync(STANDALONE_UUID_FILE, 'utf8').trim();
    if (uid) return uid;
  } catch { /* not found */ }

  const { randomUUID } = require('crypto') as typeof import('crypto');
  const uid = randomUUID() as string;
  writeFileSync(STANDALONE_UUID_FILE, uid);
  return uid;
}

// ---------------------------------------------------------------------------
// Sandbox log — so Python server.py _discover_sandbox_id() finds this daemon
// ---------------------------------------------------------------------------

function writeSandboxLog(deploymentId: string): void {
  mkdirSync(DAEMON_DIR, { recursive: true });
  appendFileSync(DAEMON_LOG, JSON.stringify({ sandboxId: deploymentId, source: 'npm-daemon' }) + '\n');
}

// ---------------------------------------------------------------------------
// Thread workspace persistence
// ---------------------------------------------------------------------------

function loadThreadWorkspaces(): Record<string, string> {
  try {
    const { readFileSync } = require('fs') as typeof import('fs');
    return JSON.parse(readFileSync(WORKSPACES_FILE, 'utf8')) as Record<string, string>;
  } catch {
    return {};
  }
}

function saveThreadWorkspaces(workspaces: Record<string, string>): void {
  mkdirSync(DAEMON_DIR, { recursive: true });
  writeFileSync(WORKSPACES_FILE, JSON.stringify(workspaces, null, 2));
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async function pollBackend(
  depId: string,
  token: string,
): Promise<{ commands: Command[]; token: string }> {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 15_000);
      const res = await fetch(
        `${NEO_API_URL}/v2/poll/${depId}?max_messages=10&wait_time=5`,
        { headers: { 'Authorization': `Bearer ${token}` }, signal: controller.signal },
      );
      clearTimeout(timeout);

      if (res.status === 401 && attempt === 0) {
        const newToken = await refreshAuthToken();
        if (newToken) { token = newToken; continue; }
        console.error(
          'ERROR: Auth rejected (401). Check NEO_SECRET_KEY is correct, ' +
          "or run 'neo-mcp login' to re-authenticate.",
        );
        return { commands: [], token };
      }
      if (res.status === 404 || !res.ok) return { commands: [], token };

      const data = await res.json() as Command[] | { messages?: Command[] };
      const commands = Array.isArray(data) ? data : (data.messages ?? []);
      return { commands, token };
    } catch {
      return { commands: [], token };
    }
  }
  return { commands: [], token };
}

async function sendResponse(depId: string, token: string, response: Record<string, unknown>): Promise<void> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30_000);
    await fetch(`${NEO_API_URL}/v2/poll/response`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...response, sandbox_id: response['sandbox_id'] ?? depId }),
      signal: controller.signal,
    });
    clearTimeout(timeout);
  } catch { /* best-effort */ }
}

// ---------------------------------------------------------------------------
// Main daemon loop
// ---------------------------------------------------------------------------

export async function runDaemon(opts: { workspace?: string; deploymentId?: string; signal?: AbortSignal } = {}): Promise<void> {
  const ws = resolve(opts.workspace ?? homedir());
  const depId = opts.deploymentId ?? getOrCreateDeploymentId();

  let token = getAuthToken();
  if (!token) {
    console.error(
      'ERROR: No auth token found.\n' +
      'Set your API key:  export NEO_SECRET_KEY=sk-v1-...\n' +
      "Or run 'neo-mcp login' to authenticate via browser OAuth.",
    );
    process.exit(1);
  }

  // Write sandbox log so server.py discovers this daemon
  writeSandboxLog(depId);

  // Write PID files: per-deployment (for is_running() checks) + global npm PID
  mkdirSync(DAEMON_DIR, { recursive: true });
  const pid = String(process.pid);
  writeFileSync(pidFileForDeployment(depId), pid);
  writeFileSync(NPM_PID_FILE, pid);

  const threadWorkspaces = loadThreadWorkspaces();

  console.log('Neo npm daemon ready');
  console.log(`  deployment_id : ${depId}`);
  console.log(`  workspace     : ${ws}`);
  console.log(`  backend       : ${NEO_API_URL}`);
  console.log(`  pid           : ${process.pid}`);
  console.log('Polling for commands...\n');

  // Cleanup on exit
  const ac = new AbortController();
  function shutdown(): void {
    console.log('\nDaemon shutting down.');
    for (const f of [pidFileForDeployment(depId), NPM_PID_FILE]) {
      try { require('fs').unlinkSync(f); } catch { /* already gone */ }
    }
    ac.abort();
  }

  // Callers (tests) can pass an AbortSignal to stop the loop without process.exit
  opts.signal?.addEventListener('abort', shutdown);

  process.on('SIGTERM', () => { shutdown(); process.exit(0); });
  process.on('SIGINT', () => { shutdown(); process.exit(0); });

  let backoff = 1_000; // ms

  while (!ac.signal.aborted) {
    const { commands, token: updatedToken } = await pollBackend(depId, token);
    token = updatedToken;

    if (commands.length > 0) {
      backoff = 1_000;
      for (const cmd of commands) {
        const tid = cmd.thread_id;
        const effectiveWs = (tid && threadWorkspaces[tid]) ? threadWorkspaces[tid] : ws;

        const result = await dispatch(cmd, effectiveWs);
        const response: Record<string, unknown> = { ...result };

        if (tid) {
          response['thread_id'] = tid;
          if (!threadWorkspaces[tid]) {
            threadWorkspaces[tid] = effectiveWs;
            saveThreadWorkspaces(threadWorkspaces);
          }
        }
        if (cmd.response_queue_name) {
          response['response_queue_name'] = cmd.response_queue_name;
        }

        await sendResponse(depId, token, response);
      }
    } else {
      // Interruptible sleep — exits immediately when signal fires
      await new Promise<void>(r => {
        const t = setTimeout(r, backoff);
        ac.signal.addEventListener('abort', () => { clearTimeout(t); r(); }, { once: true });
      });
      backoff = Math.min(backoff * 1.5, 10_000);
    }
  }
}
