/**
 * Neo npm daemon — singleton daemon architecture (mirrors VSCode extension).
 *
 * - Local IPC server on 127.0.0.1:31337 with token auth
 * - Deployment register/heartbeat lifecycle
 * - One backend poller per deployment
 * - Thread → workspace persistence for correct command routing
 */

import { randomBytes, randomUUID } from 'crypto';
import { appendFileSync, existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'fs';
import { createServer, IncomingMessage, Server, ServerResponse } from 'http';
import { tmpdir, userInfo } from 'os';
import { join, resolve } from 'path';
import { deriveDeploymentId, getAuthToken } from './auth.js';
import { NEO_API_URL } from './config.js';
import { Command, dispatch } from './executor.js';
import {
  DAEMON_DIR, DAEMON_LOG, NPM_PID_FILE, STANDALONE_UUID_FILE,
  WORKSPACES_FILE, pidFileForDeployment,
} from './paths.js';

type DeploymentRegistration = {
  deploymentId: string;
  workspaceFolder: string;
  authToken: string;
};

type PollerState = {
  aborter: AbortController;
  running: boolean;
};

type RegisterBody = {
  deploymentId: string;
  workspaceFolder: string;
  authToken: string;
};

const HOST = '127.0.0.1';
const PORT = Number(process.env['NEO_DAEMON_PORT'] ?? 31337);
const HEARTBEAT_TIMEOUT_MS = 5 * 60 * 1000;
const IDLE_SHUTDOWN_MS = 5 * 60 * 1000;
const LOCK_FILE = join(tmpdir(), `neo-poller-${userInfo().username}.lock`);
const TOKEN_FILE = join(DAEMON_DIR, 'daemon.token');
const IPC_ENABLED = process.env['NEO_DAEMON_IPC'] !== '0' && process.env['NODE_ENV'] !== 'test';

function writeSandboxLog(deploymentId: string): void {
  mkdirSync(DAEMON_DIR, { recursive: true });
  appendFileSync(DAEMON_LOG, `${JSON.stringify({ sandboxId: deploymentId, source: 'npm-daemon' })}\n`);
}

function getOrCreateDeploymentId(): string {
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

function loadThreadWorkspaces(): Record<string, string> {
  try {
    return JSON.parse(readFileSync(WORKSPACES_FILE, 'utf8')) as Record<string, string>;
  } catch {
    return {};
  }
}

function saveThreadWorkspaces(workspaces: Record<string, string>): void {
  mkdirSync(DAEMON_DIR, { recursive: true });
  writeFileSync(WORKSPACES_FILE, JSON.stringify(workspaces, null, 2));
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

async function pollBackend(depId: string, token: string): Promise<Command[]> {
  try {
    const res = await fetchWithTimeout(
      `${NEO_API_URL}/v2/poll/${depId}?max_messages=10&wait_time=5`,
      { headers: { 'Authorization': `Bearer ${token}` } },
      15_000,
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

async function sendResponse(depId: string, token: string, response: Record<string, unknown>): Promise<void> {
  try {
    await fetchWithTimeout(`${NEO_API_URL}/v2/poll/response`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...response, sandbox_id: response['sandbox_id'] ?? depId }),
    }, 30_000);
  } catch {
    // best-effort
  }
}

class NpmPollerDaemon {
  private readonly daemonToken: string;
  private readonly threadWorkspaces: Record<string, string>;
  private readonly deployments = new Map<string, DeploymentRegistration>();
  private readonly lastHeartbeat = new Map<string, number>();
  private readonly pollers = new Map<string, PollerState>();
  private readonly depPidFiles = new Set<string>();
  private server: Server | null = null;
  private heartbeatInterval: NodeJS.Timeout | null = null;
  private idleShutdownTimer: NodeJS.Timeout | null = null;
  private shuttingDown = false;
  private shutdownResolve: (() => void) | null = null;
  private readonly shutdownPromise: Promise<void>;
  private readonly ipcEnabled: boolean;

  constructor(ipcEnabled: boolean) {
    this.ipcEnabled = ipcEnabled;
    mkdirSync(DAEMON_DIR, { recursive: true });
    this.daemonToken = this.loadOrCreateToken();
    this.threadWorkspaces = loadThreadWorkspaces();
    this.shutdownPromise = new Promise<void>((resolveDone) => {
      this.shutdownResolve = resolveDone;
    });
  }

  async start(): Promise<void> {
    writeFileSync(NPM_PID_FILE, String(process.pid));
    if (!this.ipcEnabled) return;
    if (this.isAnotherDaemonRunning()) {
      throw new Error('Another daemon instance is already running');
    }
    this.writeLockFile();
    await this.startServer();
    this.startHeartbeatMonitor();
  }

  async waitForShutdown(): Promise<void> {
    return this.shutdownPromise;
  }

  registerDeployment(data: RegisterBody): void {
    const workspaceFolder = resolve(data.workspaceFolder);
    const dep: DeploymentRegistration = {
      deploymentId: data.deploymentId,
      workspaceFolder,
      authToken: data.authToken,
    };

    this.deployments.set(dep.deploymentId, dep);
    this.lastHeartbeat.set(dep.deploymentId, Date.now());
    this.startPoller(dep.deploymentId);

    const depPidFile = pidFileForDeployment(dep.deploymentId);
    writeFileSync(depPidFile, String(process.pid));
    this.depPidFiles.add(depPidFile);
    writeSandboxLog(dep.deploymentId);
  }

  unregisterDeployment(deploymentId: string): void {
    this.deployments.delete(deploymentId);
    this.lastHeartbeat.delete(deploymentId);
    this.stopPoller(deploymentId);

    const depPidFile = pidFileForDeployment(deploymentId);
    try { unlinkSync(depPidFile); } catch { /* ignore */ }
    this.depPidFiles.delete(depPidFile);

    if (this.deployments.size === 0 && !this.idleShutdownTimer) {
      this.idleShutdownTimer = setTimeout(() => {
        this.idleShutdownTimer = null;
        void this.shutdown();
      }, IDLE_SHUTDOWN_MS);
    }
  }

  heartbeat(deploymentId: string): void {
    if (this.deployments.has(deploymentId)) {
      this.lastHeartbeat.set(deploymentId, Date.now());
    }
  }

  async shutdown(): Promise<void> {
    if (this.shuttingDown) return;
    this.shuttingDown = true;
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
    if (this.idleShutdownTimer) {
      clearTimeout(this.idleShutdownTimer);
      this.idleShutdownTimer = null;
    }

    for (const deploymentId of Array.from(this.pollers.keys())) {
      this.stopPoller(deploymentId);
    }
    this.deployments.clear();
    this.lastHeartbeat.clear();

    if (this.server) {
      await new Promise<void>((resolveClose) => this.server?.close(() => resolveClose()));
      this.server = null;
    }

    try { unlinkSync(NPM_PID_FILE); } catch { /* ignore */ }
    for (const depPidFile of this.depPidFiles) {
      try { unlinkSync(depPidFile); } catch { /* ignore */ }
    }
    this.depPidFiles.clear();
    if (this.ipcEnabled) {
      try { unlinkSync(LOCK_FILE); } catch { /* ignore */ }
    }

    this.shutdownResolve?.();
  }

  private loadOrCreateToken(): string {
    if (existsSync(TOKEN_FILE)) {
      const token = readFileSync(TOKEN_FILE, 'utf8').trim();
      if (token) return token;
    }
    const token = randomBytes(32).toString('hex');
    writeFileSync(TOKEN_FILE, token, { mode: 0o600 });
    return token;
  }

  private isAnotherDaemonRunning(): boolean {
    if (!existsSync(LOCK_FILE)) return false;
    try {
      const lock = JSON.parse(readFileSync(LOCK_FILE, 'utf8')) as { pid: number };
      process.kill(lock.pid, 0);
      return true;
    } catch {
      try { unlinkSync(LOCK_FILE); } catch { /* ignore */ }
      return false;
    }
  }

  private writeLockFile(): void {
    writeFileSync(LOCK_FILE, JSON.stringify({ pid: process.pid, port: PORT, startedAt: new Date().toISOString() }));
  }

  private async startServer(): Promise<void> {
    this.server = createServer((req, res) => this.handleRequest(req, res));
    await new Promise<void>((resolveListen, rejectListen) => {
      this.server?.once('error', rejectListen);
      this.server?.listen(PORT, HOST, () => resolveListen());
    });
  }

  private startHeartbeatMonitor(): void {
    this.heartbeatInterval = setInterval(() => {
      const now = Date.now();
      for (const [deploymentId, ts] of this.lastHeartbeat.entries()) {
        if (now - ts > HEARTBEAT_TIMEOUT_MS) {
          this.unregisterDeployment(deploymentId);
        }
      }
    }, 30_000);
  }

  private authenticated(req: IncomingMessage, res: ServerResponse): boolean {
    const auth = req.headers.authorization;
    if (!auth || !auth.startsWith('Bearer ') || auth.slice(7) !== this.daemonToken) {
      this.respond(res, 401, { error: 'Invalid token' });
      return false;
    }
    return true;
  }

  private handleRequest(req: IncomingMessage, res: ServerResponse): void {
    const url = new URL(req.url ?? '/', `http://${HOST}:${PORT}`);
    if (url.pathname === '/health' && req.method === 'GET') {
      this.respond(res, 200, {
        status: 'healthy',
        deployments: this.deployments.size,
        activePollers: this.pollers.size,
        pid: process.pid,
      });
      return;
    }

    if (!this.authenticated(req, res)) return;

    if (url.pathname === '/register' && req.method === 'POST') {
      this.readJson(req, res, (body: RegisterBody) => {
        if (!body?.deploymentId || !body.workspaceFolder || !body.authToken) {
          this.respond(res, 400, { error: 'Missing required parameters' });
          return;
        }
        if (this.idleShutdownTimer) {
          clearTimeout(this.idleShutdownTimer);
          this.idleShutdownTimer = null;
        }
        this.registerDeployment(body);
        this.respond(res, 200, { success: true });
      });
      return;
    }

    if (url.pathname === '/unregister' && req.method === 'POST') {
      this.readJson(req, res, (body: { deploymentId?: string }) => {
        if (!body?.deploymentId) {
          this.respond(res, 400, { error: 'Missing deploymentId' });
          return;
        }
        this.unregisterDeployment(body.deploymentId);
        this.respond(res, 200, { success: true });
      });
      return;
    }

    if (url.pathname === '/heartbeat' && req.method === 'POST') {
      this.readJson(req, res, (body: { deploymentId?: string }) => {
        if (!body?.deploymentId) {
          this.respond(res, 400, { error: 'Missing deploymentId' });
          return;
        }
        this.heartbeat(body.deploymentId);
        this.respond(res, 200, { success: true });
      });
      return;
    }

    this.respond(res, 404, { error: 'Not found' });
  }

  private readJson(req: IncomingMessage, res: ServerResponse, cb: (body: any) => void): void {
    let data = '';
    req.on('data', (chunk: Buffer) => { data += chunk.toString(); });
    req.on('end', () => {
      try {
        cb(data ? JSON.parse(data) : {});
      } catch {
        this.respond(res, 400, { error: 'Invalid JSON' });
      }
    });
  }

  private respond(res: ServerResponse, status: number, body: unknown): void {
    if (res.headersSent || !res.writable) return;
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
  }

  private startPoller(deploymentId: string): void {
    if (this.pollers.has(deploymentId)) return;
    const poller: PollerState = { aborter: new AbortController(), running: true };
    this.pollers.set(deploymentId, poller);
    void this.pollLoop(deploymentId, poller);
  }

  private stopPoller(deploymentId: string): void {
    const poller = this.pollers.get(deploymentId);
    if (!poller) return;
    poller.running = false;
    poller.aborter.abort();
    this.pollers.delete(deploymentId);
  }

  private async pollLoop(deploymentId: string, poller: PollerState): Promise<void> {
    let backoffMs = 1_000;
    while (poller.running && !poller.aborter.signal.aborted) {
      const dep = this.deployments.get(deploymentId);
      if (!dep) return;
      const commands = await pollBackend(dep.deploymentId, dep.authToken);
      if (commands.length === 0) {
        await this.sleep(backoffMs, poller.aborter.signal);
        backoffMs = Math.min(Math.floor(backoffMs * 1.5), 10_000);
        continue;
      }

      backoffMs = 1_000;
      for (const cmd of commands) {
        const threadId = cmd.thread_id;
        if (threadId && !this.threadWorkspaces[threadId]) {
          Object.assign(this.threadWorkspaces, loadThreadWorkspaces());
        }
        const workspace = (threadId && this.threadWorkspaces[threadId]) ? this.threadWorkspaces[threadId] : dep.workspaceFolder;
        const result = await dispatch(cmd, workspace);
        const response: Record<string, unknown> = { ...result };

        if (threadId) {
          response['thread_id'] = threadId;
          if (!this.threadWorkspaces[threadId]) {
            this.threadWorkspaces[threadId] = workspace;
            saveThreadWorkspaces(this.threadWorkspaces);
          }
        }
        if (cmd.response_queue_name) {
          response['response_queue_name'] = cmd.response_queue_name;
        }
        await sendResponse(dep.deploymentId, dep.authToken, response);
      }
    }
  }

  private async sleep(ms: number, signal: AbortSignal): Promise<void> {
    await new Promise<void>((resolveSleep) => {
      const t = setTimeout(resolveSleep, ms);
      signal.addEventListener('abort', () => {
        clearTimeout(t);
        resolveSleep();
      }, { once: true });
    });
  }
}

export async function runDaemon(opts: { workspace?: string; deploymentId?: string; signal?: AbortSignal } = {}): Promise<void> {
  const workspace = resolve(opts.workspace ?? process.cwd());
  const deploymentId = opts.deploymentId ?? getOrCreateDeploymentId();
  const token = getAuthToken();
  if (!token) {
    console.error('ERROR: NEO_SECRET_KEY is not set.\nSet your API key: export NEO_SECRET_KEY=sk-v1-...');
    process.exit(1);
  }

  const daemon = new NpmPollerDaemon(IPC_ENABLED);
  await daemon.start();

  daemon.registerDeployment({
    deploymentId,
    workspaceFolder: workspace,
    authToken: token,
  });

  const stop = async (): Promise<void> => {
    await daemon.shutdown();
  };

  opts.signal?.addEventListener('abort', () => { void stop(); }, { once: true });
  process.on('SIGTERM', () => { void stop().then(() => process.exit(0)); });
  process.on('SIGINT', () => { void stop().then(() => process.exit(0)); });

  console.log('Neo npm daemon ready');
  console.log(`  deployment_id : ${deploymentId}`);
  console.log(`  workspace     : ${workspace}`);
  console.log(`  backend       : ${NEO_API_URL}`);
  console.log(`  pid           : ${process.pid}`);
  console.log(`  ipc           : ${IPC_ENABLED ? `http://${HOST}:${PORT}` : 'disabled (test mode)'}`);

  await daemon.waitForShutdown();
}
