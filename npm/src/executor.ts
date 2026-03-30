/**
 * Action handlers — mirrors Python daemon's DaemonActionHandlers exactly.
 * Supported actions: create_session, write_code, get_file, run_subprocess,
 *                    get_job_status, terminate_job, list_files
 */

import { randomUUID } from 'crypto';
import { spawn, ChildProcess } from 'child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'fs';
import { homedir, tmpdir } from 'os';
import { dirname, isAbsolute, join, resolve } from 'path';

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
  const allowed = new Set([workspace, home, tmp, resolve(tmp)].filter(Boolean));
  if (isAbsolute(pathStr)) {
    const r = resolve(pathStr);
    return [...allowed].some(a => r === a || r.startsWith(a + '/')) ? r : null;
  }
  const r = resolve(join(workspace, pathStr));
  return r === workspace || r.startsWith(workspace + '/') ? r : null;
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

function hWriteCode(cmd: Command, workspace: string): ActionResult {
  const { filename, code } = cmd;
  if (!filename || code === undefined) {
    return { request_id: cmd.request_id, status: 'error', error: 'filename and code are required' };
  }
  const workdir = cmd.workdir ?? '';
  // Use resolve() not join() so absolute workdir replaces workspace (mirrors Python os.path.join behaviour)
  const base = workdir ? (isAbsolute(workdir) ? workdir : join(workspace, workdir)) : workspace;
  const full = safeResolve(base, filename) ?? safeResolve(workspace, filename);
  if (!full) {
    console.warn(`[write_code] BLOCKED path=${filename} (outside workspace/tmp)`);
    return { request_id: cmd.request_id, status: 'error', error: `Path escapes allowed directories: ${filename}` };
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
  const fp = cmd.file_path;
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

function hRunSubprocess(cmd: Command, workspace: string): ActionResult {
  const { command } = cmd;
  if (!command) {
    return { request_id: cmd.request_id, status: 'error', error: 'command is required' };
  }
  // Ensure workspace exists before spawning — cwd must exist or spawn throws ENOENT
  mkdirSync(workspace, { recursive: true });
  console.log(`[run_subprocess] cwd=${workspace} cmd=${command.slice(0, 120)}`);
  const jobId = randomUUID();
  const proc = spawn(command, { shell: true, cwd: workspace });
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
  const { job_id } = cmd;
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
  const { job_id } = cmd;
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
  const directory = cmd.directory ?? (payload['directory'] as string | undefined) ?? workspace;
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
      case 'run_subprocess':  return hRunSubprocess(cmd, workspace);
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
