/**
 * Action handlers — mirrors Python daemon's DaemonActionHandlers exactly.
 * Supported actions: create_session, write_code, get_file, run_subprocess,
 *                    get_job_status, terminate_job, list_files
 */

import { randomUUID } from 'crypto';
import { spawn, ChildProcess } from 'child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync } from 'fs';
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

interface JobWithMeta extends Job {
  startedAt: number; // Date.now() ms
}

const _jobs = new Map<string, JobWithMeta>();
const _JOB_TTL_MS = 24 * 60 * 60 * 1_000;    // 24 hours — mirrors Python JOB_TTL
const _JOB_MAX_RUNTIME_MS = 30 * 60 * 1_000;  // 30 minutes — mirrors Python JOB_MAX_RUNTIME
const _MAX_LOG_BYTES = 10 * 1024 * 1024;       // 10 MB per stream — mirrors Python MAX_LOG_BYTES
let _cleanupCounter = 0;

function cleanupOldJobs(): void {
  const cutoff = Date.now() - _JOB_TTL_MS;
  for (const [id, job] of _jobs.entries()) {
    if (job.exitCode !== null && job.startedAt < cutoff) {
      _jobs.delete(id);
    }
  }
}

// ---------------------------------------------------------------------------
// Path safety — mirrors Python _safe_resolve()
// ---------------------------------------------------------------------------

/**
 * Resolve symlinks in a path, handling non-existent tails gracefully.
 *
 * realpathSync() throws if any component doesn't exist yet (e.g. a new file
 * about to be written).  We walk up the path to find the longest existing
 * prefix, resolve symlinks there, then re-append the non-existent tail.
 *
 * This ensures symlinks inside the workspace (e.g. outside-link → /etc) are
 * followed before the safety check, matching Python's Path.resolve() semantics.
 */
export function realResolve(p: string): string {
  const normalized = resolve(p);
  try {
    return realpathSync(normalized);
  } catch {
    // Walk up path components until we find an existing prefix to resolve.
    const parts = normalized.split('/').filter(Boolean);
    for (let i = parts.length - 1; i >= 0; i--) {
      const prefix = '/' + parts.slice(0, i + 1).join('/');
      try {
        const real = realpathSync(prefix);
        const rest = parts.slice(i + 1).join('/');
        return rest ? join(real, rest) : real;
      } catch {
        continue;
      }
    }
    return normalized;
  }
}

export function safeResolve(workspace: string, pathStr: string): string | null {
  const home = homedir();
  const tmp = tmpdir();
  // On macOS, /tmp is a symlink to /private/tmp — include both so resolve() never surprises us.
  // Use realResolve so the allowed-roots list also has symlinks followed.
  const allowed = [workspace, home, tmp, resolve(tmp)].filter(Boolean).map(p => realResolve(resolve(p)));

  function isWithin(root: string, target: string): boolean {
    const rel = relative(root, target);
    return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel));
  }

  if (isAbsolute(pathStr)) {
    // Follow symlinks before checking containment — prevents symlink escape.
    const r = realResolve(pathStr);
    return allowed.some(a => isWithin(a, r)) ? r : null;
  }
  const w = realResolve(resolve(workspace));
  // Follow symlinks in the joined path — catches symlinks inside the workspace.
  const r = realResolve(resolve(join(w, pathStr)));
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
  const _FALSY_STRINGS = new Set(['false', '0', 'no']);
  const direct = (cmd as unknown as Record<string, unknown>)[key];
  if (typeof direct === 'boolean') return direct;
  // Mirrors Python: coerce string "false"/"0"/"no" to false — backend may send strings
  if (typeof direct === 'string') return !_FALSY_STRINGS.has(direct.toLowerCase());
  const nested = cmd.payload?.[key];
  if (typeof nested === 'boolean') return nested;
  if (typeof nested === 'string') return !_FALSY_STRINGS.has(nested.toLowerCase());
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

export function remapToWorkspace(absPath: string, workspace: string, workdir: string, stripProjectWrapper = false, isWorkdir = false): string {
  let relative: string | null = null;
  let stripableRoot = false;

  // Try workdir as the container root (e.g. workdir=/app/project/myproj, path=.../myproj/src/main.py)
  if (workdir && isAbsolute(workdir)) {
    const wd = resolve(workdir);
    if (absPath.startsWith(wd + '/')) relative = absPath.slice(wd.length + 1);
  }

  // Try /app/project first (tracked separately to keep most-specific match precedence)
  if (relative === null) {
    if (absPath === '/app/project') { relative = ''; stripableRoot = true; }
    else if (absPath.startsWith('/app/project/')) {
      relative = absPath.slice('/app/project/'.length);
      stripableRoot = true;
    }
  }

  // Try remaining known backend container roots — these are also strip-eligible
  // because the backend wraps files under <container_root>/<project_name>/.
  if (relative === null) {
    for (const root of ['/app', '/workspace', '/project']) {
      if (absPath === root) { relative = ''; stripableRoot = true; break; }
      if (absPath.startsWith(root + '/')) {
        relative = absPath.slice(root.length + 1);
        stripableRoot = true;
        break;
      }
    }
  }

  // Last resort: preserve just the filename
  if (relative === null) return join(workspace, absPath.split('/').pop() ?? absPath);

  if (relative) {
    const slashIdx = relative.indexOf('/');
    const firstSeg = slashIdx >= 0 ? relative.slice(0, slashIdx) : relative;
    const wsName = workspace.replace(/\/$/, '').split('/').pop() ?? '';

    if (stripProjectWrapper && stripableRoot && firstSeg) {
      // Strip the project-name wrapper (first segment after the container root).
      // The backend always wraps files under <container_root>/{project-name}/.
      //
      // For filenames (isWorkdir=false): only strip when 2+ segments exist —
      // a single segment like /app/model.py is the filename itself, keep it.
      // For workdirs (isWorkdir=true): always strip — a single segment like
      // /app/test_2 is the project root directory, maps to workspace.
      const shouldStrip = isWorkdir || slashIdx >= 0;
      if (shouldStrip) {
        console.error(`[remap] stripping project wrapper "${firstSeg}" from ${absPath} (local workspace name is "${wsName}")`);
        relative = slashIdx >= 0 ? relative.slice(slashIdx + 1) : '';
      }
    } else if (firstSeg && wsName === firstSeg) {
      // Legacy dedup: strip only when workspace name matches the first segment.
      // Used by remapCommandPaths where segments may be real subdirectory names.
      relative = slashIdx >= 0 ? relative.slice(slashIdx + 1) : '';
    }
  }

  return relative ? join(workspace, relative) : workspace;
}

/**
 * Replace known container-root paths in a shell command string with the local
 * workspace equivalent — mirrors Python ActionHandlers._remap_command_paths().
 *
 * The Neo backend constructs shell commands using its own container paths
 * (e.g. `ls /app/project/foo`).  Without remapping, those paths don't exist on
 * the host and the command fails.  Roots are tried longest-first so
 * /app/project is matched before /app.
 */
export function remapCommandPaths(command: string, workspace: string): string {
  const roots = ['/app/project', '/workspace', '/project', '/app'];
  let result = command;
  for (const root of roots) {
    const escapedRoot = root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    // Match root + optional path continuation (stop at shell metacharacters / whitespace)
    const re = new RegExp(escapedRoot + '(/[^\\s\'"`;|&<>(){}\\[\\]\\\\]*)?', 'g');
    result = result.replace(re, (match) => {
      const trailingSlash = match.endsWith('/');
      // Mirror write_code's wrapper-stripping: Neo always wraps its files under
      // <container_root>/<project-name>/, so `ls /app/<proj>/data/` must resolve
      // to `<workspace>/data/` — not `<workspace>/<proj>/data/`. Without this,
      // write_code lands at `<workspace>/data/x.txt` but Neo's verify subprocess
      // looks at `<workspace>/<proj>/data/x.txt` (wrong) and retries forever.
      const remapped = remapToWorkspace(trailingSlash ? match.slice(0, -1) : match, workspace, '', true);
      return trailingSlash && !remapped.endsWith('/') ? remapped + '/' : remapped;
    });
  }
  return result;
}

// Per-thread Neo project slug (e.g. "movie_recommender_system_1703"), captured the
// first time we see an absolute container path for the thread. Used to rewrite
// *relative* wrapper references Neo embeds inside shell scripts (`mkdir -p <slug>/data`)
// — those aren't caught by remapCommandPaths because there's no syntactic marker.
const _threadWrappers = new Map<string, string>();

const _CONTAINER_ROOTS = ['/app/project', '/app', '/workspace', '/project'];

export function extractWrapper(absPath: string): string | null {
  for (const root of _CONTAINER_ROOTS) {
    if (absPath === root) return null;
    if (absPath.startsWith(root + '/')) {
      const rel = absPath.slice(root.length + 1);
      const slashIdx = rel.indexOf('/');
      return slashIdx > 0 ? rel.slice(0, slashIdx) : null; // need wrapper + something after
    }
  }
  return null;
}

function recordWrapper(threadId: string | undefined, absPath: string): void {
  if (!threadId || _threadWrappers.has(threadId)) return;
  const slug = extractWrapper(absPath);
  if (slug) {
    _threadWrappers.set(threadId, slug);
    console.error(`[wrapper] recorded Neo project wrapper for thread ${threadId}: ${slug}`);
  }
}

export function stripWrapperPrefixes(text: string, wrapper: string): string {
  const escaped = wrapper.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // Strip "<wrapper>/" (with anything following); negative lookbehind to avoid mid-word matches.
  text = text.replace(new RegExp(`(?<![A-Za-z0-9_])${escaped}/`, 'g'), '');
  // Bare "<wrapper>" token: substitute "." so `cd <wrapper>` → `cd .`.
  text = text.replace(new RegExp(`(?<![A-Za-z0-9_])${escaped}(?![A-Za-z0-9_])`, 'g'), '.');
  return text;
}

function applyWrapperRewrite(text: string, threadId: string | undefined): string {
  const slug = threadId ? _threadWrappers.get(threadId) : undefined;
  if (!slug) return text;
  const rewritten = stripWrapperPrefixes(text, slug);
  if (rewritten !== text) {
    console.error(`[wrapper] stripped ${slug} from ${text.length} chars of shell text`);
  }
  return rewritten;
}

// Test-only: reset per-thread wrapper state between assertions.
export function _resetWrappersForTests(): void {
  _threadWrappers.clear();
}

function hWriteCode(cmd: Command, workspace: string): ActionResult {
  let filename = fieldString(cmd, 'filename');
  let code = fieldString(cmd, 'code') ?? (typeof cmd.code === 'string' ? cmd.code : undefined);
  if (!filename || code === undefined) {
    return { request_id: cmd.request_id, status: 'error', error: 'filename and code are required' };
  }
  const workdir = fieldString(cmd, 'workdir') ?? '';

  console.error(`[write_code] filename=${filename} workdir=${workdir} workspace=${workspace}`);

  // Normalize container-relative filenames to absolute paths so they go through
  // the standard remap logic below. Backend sometimes omits the leading '/'
  // (e.g. "app/project/myproj/model.py") which would otherwise land verbatim
  // under the workspace (workspace/app/project/myproj/model.py).
  const CONTAINER_REL_PREFIXES = ['app/project/', 'app/', 'workspace/', 'project/'];
  if (!isAbsolute(filename) && CONTAINER_REL_PREFIXES.some(p => filename!.startsWith(p))) {
    console.error(`[write_code] normalized container-relative filename "${filename}" → "/${filename}"`);
    filename = '/' + filename;
  }

  let full: string;

  if (isAbsolute(filename)) {
    const resolved = resolve(filename);
    // Opportunistically learn Neo's project-slug for this thread so scripts
    // written later can have their relative <slug>/ references rewritten.
    recordWrapper(cmd.thread_id, resolved);
    // If the path is already inside the local workspace or /tmp, use it as-is.
    // Otherwise it's a backend container path (e.g. /app/project/src/main.py) — remap.
    const direct = safeResolve(workspace, filename);
    full = direct ?? remapToWorkspace(resolved, workspace, workdir, true);
    console.error(`[write_code] remapped ${filename} → ${full}`);
  } else {
    // Relative filename: if workdir is absolute (backend container path like /app/project/test_2/demo),
    // remap it to the local workspace to preserve subdirectory structure.
    // e.g. workdir=/app/project/test_2/demo → base=<workspace>/demo (project wrapper stripped)
    //
    // If workdir is relative (e.g. "multimodal_rag_0345" or "multimodal_rag_0345/src"),
    // strip the first segment — it is always the project-name wrapper, same as the first
    // segment after /app/project/ in absolute paths.
    const base = workdir
      ? isAbsolute(workdir)
        ? remapToWorkspace(resolve(workdir), workspace, '', true, true)
        : (() => {
            const slashIdx = workdir.indexOf('/');
            const rest = slashIdx >= 0 ? workdir.slice(slashIdx + 1) : '';
            if (rest) console.error(`[write_code] relative workdir: stripped project wrapper "${workdir.slice(0, slashIdx)}" → base subdir "${rest}"`);
            return rest ? join(workspace, rest) : workspace;
          })()
      : workspace;
    const candidate = safeResolve(base, filename) ?? safeResolve(workspace, filename);
    if (!candidate) {
      console.error(`[write_code] BLOCKED path=${filename} (outside workspace/tmp)`);
      return { request_id: cmd.request_id, status: 'error', error: `Path escapes allowed directories: ${filename}` };
    }
    full = candidate;
  }

  // Rewrite Neo's relative <slug>/ references inside shell scripts. Without this a
  // script like `mkdir -p <slug>/data` creates <workspace>/<slug>/data when the daemon
  // runs it with cwd=<workspace> — the slug was meant relative to Neo's container cwd
  // (/app/<slug>/) and has no meaning on the host.
  if (full.endsWith('.sh') || full.endsWith('.bash') || code.startsWith('#!')) {
    code = applyWrapperRewrite(code, cmd.thread_id);
  }

  mkdirSync(dirname(full), { recursive: true });
  writeFileSync(full, code, 'utf8');
  console.error(`[write_code] wrote ${full}`);
  return {
    request_id: cmd.request_id,
    status: 'success',
    // workdir: empty string when not provided — mirrors Python: "workdir": workdir or ""
    data: { file_path: full, workdir: workdir || '' },
  };
}

function hGetFile(cmd: Command, workspace: string): ActionResult {
  let fp = fieldString(cmd, 'file_path');
  if (!fp) {
    return { request_id: cmd.request_id, status: 'error', error: 'file_path is required' };
  }
  // Normalize container-relative paths (same logic as hWriteCode).
  const CONTAINER_REL_PREFIXES = ['app/project/', 'app/', 'workspace/', 'project/'];
  if (!isAbsolute(fp) && CONTAINER_REL_PREFIXES.some(p => fp!.startsWith(p))) {
    console.error(`[get_file] normalized container-relative path "${fp}" → "/${fp}"`);
    fp = '/' + fp;
  }
  if (isAbsolute(fp)) {
    recordWrapper(cmd.thread_id, resolve(fp));
  }
  // Try direct resolution first; if outside workspace (backend container path), remap it.
  let full = safeResolve(workspace, fp);
  if (!full && isAbsolute(fp)) {
    full = remapToWorkspace(resolve(fp), workspace, '', true);
    console.error(`[get_file] remapped ${fp} → ${full}`);
  }
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

  // Pre-flight: if the backend sent a /tmp script path, verify it exists locally
  // before spawning. The backend must send write_code first; if it didn't (e.g.
  // a race or ordering bug), fail fast with a clear error rather than a cryptic
  // shell error — mirrors Python action_handlers._run_subprocess lines 210-222.
  const scriptMatch = command.match(/(?:bash|sh)\s+(\/tmp\/bash_exec_[a-f0-9]+\.sh)/);
  if (scriptMatch) {
    const scriptPath = scriptMatch[1];
    if (!existsSync(scriptPath)) {
      console.error(`[run_subprocess] Script not found locally: ${scriptPath}`);
      return {
        request_id: cmd.request_id,
        status: 'error',
        error: `Script not found: ${scriptPath}. Backend must send 'write_code' before 'run_subprocess'.`,
      };
    }
  }

  const detach = fieldBoolean(cmd, 'detach', true);
  const cmdWorkdir = fieldString(cmd, 'workdir');
  // Ignore absolute workdir from backend container — always run in local workspace.
  const cwd = cmdWorkdir && !isAbsolute(cmdWorkdir) ? join(workspace, cmdWorkdir) : workspace;
  const safeCwd = safeResolve(workspace, cwd) ?? workspace;

  // Remap container paths in the command string so shell commands like
  // `ls /app/project/foo` work on the host filesystem — mirrors Python
  // ActionHandlers._remap_command_paths().
  let remappedCommand = remapCommandPaths(command, workspace);
  if (remappedCommand !== command) {
    console.error(`[run_subprocess] remapped paths: ${command.slice(0, 80)} → ${remappedCommand.slice(0, 80)}`);
  }
  // Strip relative wrapper references (`cd <slug>`, `mkdir <slug>/data`) for which
  // remapCommandPaths can't help — they're syntactically indistinguishable from real
  // subdirs. Uses the slug captured from earlier absolute writes on this thread.
  remappedCommand = applyWrapperRewrite(remappedCommand, cmd.thread_id);

  // Ensure workspace exists before spawning — cwd must exist or spawn throws ENOENT
  mkdirSync(safeCwd, { recursive: true });
  console.error(`[run_subprocess] cwd=${safeCwd} cmd=${remappedCommand.slice(0, 120)} detach=${detach}`);

  if (!detach) {
    const proc = spawn(remappedCommand, { shell: true, cwd: safeCwd });
    let stdout = '';
    let stderr = '';
    proc.stdout?.on('data', (chunk: Buffer) => { stdout += chunk.toString(); });
    proc.stdout?.on('error', () => { /* ignore stream errors */ });
    proc.stderr?.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
    proc.stderr?.on('error', () => { /* ignore stream errors */ });

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
  const proc = spawn(remappedCommand, { shell: true, cwd: safeCwd });
  const job: JobWithMeta = { proc, stdout: '', stderr: '', exitCode: null, startedAt: Date.now() };
  _jobs.set(jobId, job);

  // Stream output with per-stream size cap — mirrors Python JobManager MAX_LOG_BYTES.
  // Truncates the oldest 20% when the cap is exceeded, keeping the most recent output.
  function appendCapped(field: 'stdout' | 'stderr', text: string): void {
    job[field] += text;
    if (job[field].length > _MAX_LOG_BYTES) {
      job[field] = job[field].slice(-Math.floor(_MAX_LOG_BYTES * 0.8));
    }
  }
  proc.stdout?.on('data', (chunk: Buffer) => appendCapped('stdout', chunk.toString()));
  proc.stdout?.on('error', () => { /* ignore stream errors */ });
  proc.stderr?.on('data', (chunk: Buffer) => appendCapped('stderr', chunk.toString()));
  proc.stderr?.on('error', () => { /* ignore stream errors */ });

  // Kill hung jobs after 30 minutes — mirrors Python JOB_MAX_RUNTIME / asyncio.timeout().
  const killTimer = setTimeout(() => {
    if (job.exitCode === null) {
      console.error(`[run_subprocess] Job ${jobId} exceeded max runtime — killing`);
      job.stderr += `\n[Killed: exceeded ${_JOB_MAX_RUNTIME_MS / 60_000}min max runtime]`;
      try { proc.kill('SIGKILL'); } catch { /* already gone */ }
    }
  }, _JOB_MAX_RUNTIME_MS);

  proc.on('close', (code: number | null) => {
    clearTimeout(killTimer);
    job.exitCode = code ?? -1;
  });

  // Periodic cleanup every 200 jobs to prevent unbounded memory growth
  if (++_cleanupCounter % 200 === 0) cleanupOldJobs();

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
  if (job.exitCode !== null) {
    // Already completed — matches Python terminate_job early-return for completed jobs.
    return { request_id: cmd.request_id, status: 'success', data: { job_id, terminated: true } };
  }
  try { job.proc.kill('SIGTERM'); } catch { /* already exited */ }
  job.stderr += '\n[terminated by daemon]';
  // Schedule SIGKILL after 5 s in case SIGTERM is ignored — mirrors Python _force_kill().
  setTimeout(() => { try { job.proc.kill('SIGKILL'); } catch { /* already gone */ } }, 5_000);
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

  let target: string;
  if (isAbsolute(directory)) {
    // If outside workspace (backend container path like /app/project), remap to local workspace.
    // isWorkdir=true so a single-segment wrapper like /app/myproj_0001 maps to workspace root
    // rather than a wrapper subfolder that doesn't exist on disk.
    const direct = safeResolve(workspace, directory);
    if (direct) {
      target = direct;
    } else {
      target = remapToWorkspace(resolve(directory), workspace, '', true, true);
      console.error(`[list_files] remapped ${directory} → ${target}`);
    }
  } else {
    target = safeResolve(workspace, directory) ?? workspace;
  }

  if (!existsSync(target) || !statSync(target).isDirectory()) {
    return { request_id: cmd.request_id, status: 'error', error: `Directory not found: ${directory}` };
  }

  // Target directory itself is NOT included — matches VS Code extension and Python daemon.
  const lines: string[] = [];

  function walk(dir: string, depth: number): void {
    if (depth > maxDepth) return;
    let entries: import('fs').Dirent[];
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    // Dirs first, then files — each group sorted alphabetically.
    // Mirrors VS Code DaemonActionHandlers and Python action_handlers.
    const dirs = entries.filter(e => e.isDirectory() && (includeHidden || !e.name.startsWith('.'))).sort((a, b) => a.name.localeCompare(b.name));
    const files = entries.filter(e => e.isFile() && (includeHidden || !e.name.startsWith('.'))).sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of [...dirs, ...files]) {
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
  console.error(`[dispatch] action=${cmd.action} request_id=${cmd.request_id} thread_id=${cmd.thread_id ?? 'none'}`);
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
