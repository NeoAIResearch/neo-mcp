/**
 * Action handlers — mirrors Python daemon's DaemonActionHandlers exactly.
 * Supported actions: create_session, write_code, get_file, run_subprocess,
 *                    get_job_status, terminate_job, list_files
 */

import { randomUUID } from 'crypto';
import { spawn, ChildProcess } from 'child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'fs';
import { homedir, tmpdir } from 'os';
import { dirname, isAbsolute, join, relative, resolve } from 'path';

// Directories skipped during file listing — matches Python/TS daemon
const SKIP_DIRS = new Set([
  'venv', 'node_modules', 'env', '.venv', '__pycache__', '.git',
  '.tox', 'dist', 'build',
]);

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Command {
  action: string;
  request_id: string;
  thread_id?: string;
  response_queue_name?: string;
  // action-specific fields
  session_id?: string;
  payload?: Record<string, unknown>;
  filename?: string;
  code?: string;
  workdir?: string;
  file_path?: string;
  command?: string;
  job_id?: string;
  directory?: string;
  max_depth?: number;
  include_hidden?: boolean;
}

export interface ActionResult {
  request_id: string;
  status: string;
  data?: Record<string, unknown>;
  error?: string;
}

interface Job {
  proc: ChildProcess;
  stdout: string;
  stderr: string;
  exitCode: number | null;
}

// ---------------------------------------------------------------------------
// Job registry
// ---------------------------------------------------------------------------

const _jobs = new Map<string, Job>();

// ---------------------------------------------------------------------------
// Path safety — mirrors Python _safe_resolve()
// ---------------------------------------------------------------------------

export function safeResolve(workspace: string, pathStr: string): string | null {
  const home = homedir();
  const tmp = tmpdir();
  // On macOS, /tmp is a symlink to /private/tmp — include both so resolve() never surprises us
  const allowed = [workspace, home, tmp, resolve(tmp)].filter(Boolean).map(p => resolve(p));

  function isWithin(root: string, target: string): boolean {
    const rel = relative(root, target);
    return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel));
  }

  if (isAbsolute(pathStr)) {
    const r = resolve(pathStr);
    return allowed.some(a => isWithin(a, r)) ? r : null;
  }
  const w = resolve(workspace);
  const r = resolve(join(w, pathStr));
  return isWithin(w, r) ? r : null;
}

function fieldString(cmd: Command, key: string): string | undefined {
  const direct = (cmd as unknown as Record<string, unknown>)[key];
  if (typeof direct === 'string' && direct.length > 0) return direct;
  const nested = cmd.payload?.[key];
  if (typeof nested === 'string' && nested.length > 0) return nested;
  return undefined;
}

function fieldBoolean(cmd: Command, key: string, fallback: boolean): boolean {
  const direct = (cmd as unknown as Record<string, unknown>)[key];
  if (typeof direct === 'boolean') return direct;
  const nested = cmd.payload?.[key];
  if (typeof nested === 'boolean') return nested;
  return fallback;
}

// ---------------------------------------------------------------------------
// Action handlers
// ---------------------------------------------------------------------------

function hCreateSession(cmd: Command): ActionResult {
  const sid =
    cmd.session_id ??
    (cmd.payload?.['session_id'] as string | undefined) ??
    randomUUID();
  return {
    request_id: cmd.request_id,
    status: 'success',
    data: { coding_session_id: sid },
  };
}

function remapToWorkspace(absPath: string, workspace: string, workdir: string): string {
  let relative: string | null = null;

  // Try workdir as the container root (e.g. workdir=/app/project, path=/app/project/src/main.py)
  if (workdir && isAbsolute(workdir)) {
    const wd = resolve(workdir);
    if (absPath.startsWith(wd + '/')) relative = absPath.slice(wd.length + 1);
  }

  // Try known backend container roots
  if (relative === null) {
    for (const root of ['/app/project', '/app', '/workspace', '/project']) {
      if (absPath.startsWith(root + '/')) {
        relative = absPath.slice(root.length + 1);
        break;
      }
    }
  }

  // Last resort: preserve just the filename
  if (relative === null) return join(workspace, absPath.split('/').pop() ?? absPath);

  // Deduplicate: if workspace ends with the first path segment of relative,
  // the user's workspace IS that directory — don't nest it again.
  // e.g. workspace=/project/test_2, relative=test_2/file.py → file.py
  const wsBase = workspace.endsWith('/') ? workspace.slice(0, -1) : workspace;
  const slashIdx = relative.indexOf('/');
  const firstSeg = slashIdx >= 0 ? relative.slice(0, slashIdx) : relative;
  if (firstSeg && wsBase.endsWith('/' + firstSeg)) {
    relative = slashIdx >= 0 ? relative.slice(slashIdx + 1) : '';
  }

  return relative ? join(workspace, relative) : workspace;
}

function hWriteCode(cmd: Command, workspace: string): ActionResult {
  const filename = fieldString(cmd, 'filename');
  const code = fieldString(cmd, 'code') ?? (typeof cmd.code === 'string' ? cmd.code : undefined);
  if (!filename || code === undefined) {
    return { request_id: cmd.request_id, status: 'error', error: 'filename and code are required' };
  }
  const workdir = fieldString(cmd, 'workdir') ?? '';

  let full: string;

  if (isAbsolute(filename)) {
    const resolved = resolve(filename);
    // If the path is already inside the local workspace or /tmp, use it as-is.
    // Otherwise it's a backend container path (e.g. /app/project/src/main.py) — remap.
    const direct = safeResolve(workspace, filename);
    full = direct ?? remapToWorkspace(resolved, workspace, workdir);
    console.log(`[write_code] remapped ${filename} → ${full}`);
  } else {
    // Relative filename: if workdir is absolute (backend container path like /app/project/test_2/demo),
    // remap it to the local workspace to preserve subdirectory structure.
    // e.g. workdir=/app/project/test_2/demo → base=<workspace>/test_2/demo
    const base = workdir
      ? isAbsolute(workdir)
        ? remapToWorkspace(resolve(workdir), workspace, '')
        : join(workspace, workdir)
      : workspace;
    const candidate = safeResolve(base, filename) ?? safeResolve(workspace, filename);
    if (!candidate) {
      console.warn(`[write_code] BLOCKED path=${filename} (outside workspace/tmp)`);
      return { request_id: cmd.request_id, status: 'error', error: `Path escapes allowed directories: ${filename}` };
    }
    full = candidate;
  }

  mkdirSync(dirname(full), { recursive: true });
  writeFileSync(full, code, 'utf8');
  console.log(`[write_code] wrote ${full}`);
  return {
    request_id: cmd.request_id,
    status: 'success',
    data: { file_path: full, workdir: workdir || workspace },
  };
}

function hGetFile(cmd: Command, workspace: string): ActionResult {
  const fp = fieldString(cmd, 'file_path');
  if (!fp) {
    return { request_id: cmd.request_id, status: 'error', error: 'file_path is required' };
  }
  const full = safeResolve(workspace, fp);
  if (!full || !existsSync(full) || !statSync(full).isFile()) {
    return { request_id: cmd.request_id, status: 'error', error: `File not found: ${fp}` };
  }
  const content = readFileSync(full, 'utf8');
  return {
    request_id: cmd.request_id,
    status: 'success',
    data: { file_content: content, file_path: full },
  };
}

async function hRunSubprocess(cmd: Command, workspace: string): Promise<ActionResult> {
  const command = fieldString(cmd, 'command');
  if (!command) {
    return { request_id: cmd.request_id, status: 'error', error: 'command is required' };
  }

  const detach = fieldBoolean(cmd, 'detach', true);
  const cmdWorkdir = fieldString(cmd, 'workdir');
  // Ignore absolute workdir from backend container — always run in local workspace.
  const cwd = cmdWorkdir && !isAbsolute(cmdWorkdir) ? join(workspace, cmdWorkdir) : workspace;
  const safeCwd = safeResolve(workspace, cwd) ?? workspace;

  // Ensure workspace exists before spawning — cwd must exist or spawn throws ENOENT
  mkdirSync(safeCwd, { recursive: true });
  console.log(`[run_subprocess] cwd=${safeCwd} cmd=${command.slice(0, 120)} detach=${detach}`);

  if (!detach) {
    const proc = spawn(command, { shell: true, cwd: safeCwd });
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (chunk: Buffer) => { stdout += chunk.toString(); });
    proc.stderr.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });

    const exitCode = await new Promise<number>((resolveExit) => {
      proc.on('close', (code: number | null) => resolveExit(code ?? -1));
      proc.on('error', () => resolveExit(-1));
    });

    return {
      request_id: cmd.request_id,
      status: exitCode === 0 ? 'completed' : 'error',
      data: {
        detached: false,
        completed: true,
        exit_code: exitCode,
        stdout,
        stderr,
      },
      ...(exitCode === 0 ? {} : { error: `Command failed with exit code ${exitCode}` }),
    };
  }

  const jobId = randomUUID();
  const proc = spawn(command, { shell: true, cwd: safeCwd });
  const job: Job = { proc, stdout: '', stderr: '', exitCode: null };
  _jobs.set(jobId, job);

  proc.stdout.on('data', (chunk: Buffer) => { job.stdout += chunk.toString(); });
  proc.stderr.on('data', (chunk: Buffer) => { job.stderr += chunk.toString(); });
  proc.on('close', (code: number | null) => { job.exitCode = code ?? -1; });

  return {
    request_id: cmd.request_id,
    status: 'success',
    data: { job_id: jobId, detached: true, message: 'Job started in background' },
  };
}

function hGetJobStatus(cmd: Command): ActionResult {
  const job_id = fieldString(cmd, 'job_id');
  const job = job_id ? _jobs.get(job_id) : undefined;
  if (!job) {
    return { request_id: cmd.request_id, status: 'error', error: `Job not found: ${job_id ?? ''}` };
  }
  const done = job.exitCode !== null;
  return {
    request_id: cmd.request_id,
    status: done ? 'completed' : 'pending',
    data: {
      job_id,
      stdout: job.stdout,
      stderr: job.stderr,
      exit_code: job.exitCode,
      completed: done,
    },
  };
}

function hTerminateJob(cmd: Command): ActionResult {
  const job_id = fieldString(cmd, 'job_id');
  const job = job_id ? _jobs.get(job_id) : undefined;
  if (!job) {
    return { request_id: cmd.request_id, status: 'error', error: `Job not found: ${job_id ?? ''}` };
  }
  try { job.proc.kill('SIGTERM'); } catch { /* already exited */ }
  job.exitCode = -15; // SIGTERM
  job.stderr += '\n[terminated by daemon]';
  return {
    request_id: cmd.request_id,
    status: 'success',
    data: { job_id, terminated: true },
  };
}

function hListFiles(cmd: Command, workspace: string): ActionResult {
  const payload = cmd.payload ?? {};
  const directory = fieldString(cmd, 'directory') ?? (payload['directory'] as string | undefined) ?? workspace;
  const maxDepth = Number(cmd.max_depth ?? payload['max_depth'] ?? 10);
  const includeHidden = Boolean(cmd.include_hidden ?? payload['include_hidden'] ?? false);

  const target = isAbsolute(directory)
    ? resolve(directory)
    : (safeResolve(workspace, directory) ?? workspace);

  if (!existsSync(target) || !statSync(target).isDirectory()) {
    return { request_id: cmd.request_id, status: 'error', error: `Directory not found: ${directory}` };
  }

  const lines: string[] = [`${target}|d|0`];

  function walk(dir: string, depth: number): void {
    if (depth > maxDepth) return;
    let entries: import('fs').Dirent[];
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      if (!includeHidden && entry.name.startsWith('.')) continue;
      const fullPath = join(dir, entry.name);
      if (entry.isDirectory()) {
        lines.push(`${fullPath}|d|0`);
        if (!SKIP_DIRS.has(entry.name)) walk(fullPath, depth + 1);
      } else {
        let size = 0;
        try { size = statSync(fullPath).size; } catch { /* ignore */ }
        lines.push(`${fullPath}|f|${size}`);
      }
    }
  }

  walk(target, 1);
  return {
    request_id: cmd.request_id,
    status: 'success',
    data: { stdout: lines.join('\n'), file_count: lines.length, directory: target },
  };
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

export async function dispatch(cmd: Command, workspace: string): Promise<ActionResult> {
  console.log(`[dispatch] action=${cmd.action} request_id=${cmd.request_id}`);
  try {
    switch (cmd.action) {
      case 'create_session':  return hCreateSession(cmd);
      case 'write_code':      return hWriteCode(cmd, workspace);
      case 'get_file':        return hGetFile(cmd, workspace);
      case 'run_subprocess':  return await hRunSubprocess(cmd, workspace);
      case 'get_job_status':  return hGetJobStatus(cmd);
      case 'terminate_job':   return hTerminateJob(cmd);
      case 'list_files':      return hListFiles(cmd, workspace);
      default:
        return { request_id: cmd.request_id, status: 'error', error: `Unknown action: ${cmd.action}` };
    }
  } catch (err) {
    console.error(`[dispatch] error in ${cmd.action}:`, err);
    return { request_id: cmd.request_id, status: 'error', error: String(err) };
  }
}
