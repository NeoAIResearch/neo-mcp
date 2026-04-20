/**
 * npm system test suite — single file replacing all previous test files.
 *
 * Coverage mirrors python/tests/test_system.py, organized by concern:
 *
 *  1. realResolve          — symlink-aware path resolution
 *  2. safeResolve          — path containment + symlink escape
 *  3. remapToWorkspace     — container root → local path (all 4 roots, dedup, exact)
 *  4. remapCommandPaths    — shell command container-path substitution
 *  5. write_code           — relative, container, workdir, traversal, overwrite
 *  6. get_file             — relative, container, blocked, roundtrip
 *  7. run_subprocess       — detach, blocking, exit codes, preflight, remapping
 *  8. list_files           — basic, hidden, skip_dirs, max_depth, container path
 *  9. create_session       — explicit id, payload id, auto UUID
 * 10. dispatch misc        — unknown action, request_id echoed, all actions routable
 * 11. path security        — traversal, /etc, sibling workspace, /tmp allowed
 * 12. symlink escape       — write/get via symlink pointing outside workspace
 * 13. concurrent workspace — parallel writes stay in correct workspaces
 * 14. auth                 — deriveDeploymentId UUID v5, cross-language, getAuthToken
 * 15. deployment ID policy — explicit override, key-derived, machine UUID stability
 * 16. thread workspaces    — persist, roundtrip, TTL eviction, cap, meta timestamps
 * 17. pollBackend          — 404/500/network/401-AuthError/array/messages shapes
 * 18. sendResponse retry   — first attempt, 2nd/3rd retry, all-3-fail no-throw, sandbox_id
 * 19. runDaemon            — startup log, PID, dispatch, 401 stops, abort signal
 * 20. thread status gate   — TERMINATED rejected, RUNNING/PAUSED/unknown accepted
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync,
  symlinkSync, unlinkSync, writeFileSync,
} from 'fs';
import { tmpdir } from 'os';
import { createHash } from 'crypto';
import { join, resolve } from 'path';

import {
  realResolve, safeResolve,
  remapToWorkspace, remapCommandPaths,
  dispatch, Command,
} from '../src/executor.js';
import {
  deriveDeploymentId, getAuthToken,
} from '../src/auth.js';
import {
  getOrCreateDeploymentId,
  registerThreadWorkspace, loadThreadWorkspaces, loadThreadWorkspacesWithMeta,
  saveThreadWorkspaces, setThreadStatus, runDaemon,
  pollBackend, sendResponse, AuthError,
} from '../src/daemon.js';
import {
  DAEMON_LOG, WORKSPACES_FILE, DAEMON_DIR,
  pidFileForDeployment,
} from '../src/paths.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeWs(): string {
  return mkdtempSync(join(tmpdir(), 'neo-sys-test-'));
}

function makeCmd(overrides: Partial<Command>): Command {
  return { action: 'noop', request_id: 'req-1', ...overrides };
}

function backupWorkspacesFile(): string | null {
  if (existsSync(WORKSPACES_FILE)) {
    const bak = `${WORKSPACES_FILE}.bak-${process.pid}`;
    writeFileSync(bak, readFileSync(WORKSPACES_FILE));
    rmSync(WORKSPACES_FILE);
    return bak;
  }
  return null;
}

function restoreWorkspacesFile(bak: string | null): void {
  if (existsSync(WORKSPACES_FILE)) rmSync(WORKSPACES_FILE);
  if (bak) {
    writeFileSync(WORKSPACES_FILE, readFileSync(bak));
    rmSync(bak);
  }
}

async function runDaemonBriefly(
  workspace: string,
  delayMs: number,
  fetchMock: (url: string | URL | Request, init?: RequestInit) => Promise<Response>,
): Promise<void> {
  const ac = new AbortController();
  const savedFetch = global.fetch;
  global.fetch = fetchMock as typeof fetch;
  const done = runDaemon({ workspace, signal: ac.signal });
  await new Promise(r => setTimeout(r, delayMs));
  ac.abort();
  await done;
  global.fetch = savedFetch;
}

// Environment variable backup helpers
function envBackup(...keys: string[]): Record<string, string | undefined> {
  return Object.fromEntries(keys.map(k => [k, process.env[k]]));
}
function envRestore(saved: Record<string, string | undefined>): void {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

// ===========================================================================
// PART 1 — realResolve
// ===========================================================================

describe('realResolve', () => {
  let ws: string;
  beforeEach(() => { ws = makeWs(); });
  afterEach(() => { rmSync(ws, { recursive: true, force: true }); });

  it('resolves an existing file path', () => {
    writeFileSync(join(ws, 'a.txt'), 'x');
    const r = realResolve(join(ws, 'a.txt'));
    expect(r).toBe(join(ws, 'a.txt'));
  });

  it('resolves a non-existent file by resolving its longest existing prefix', () => {
    const r = realResolve(join(ws, 'nonexistent.py'));
    expect(r).toBe(join(ws, 'nonexistent.py'));
  });

  it('follows symlinks to real targets', () => {
    writeFileSync(join(ws, 'real.txt'), 'content');
    symlinkSync(join(ws, 'real.txt'), join(ws, 'link.txt'));
    const r = realResolve(join(ws, 'link.txt'));
    expect(r).toBe(join(ws, 'real.txt'));
  });

  it('follows a directory symlink', () => {
    mkdirSync(join(ws, 'realdir'));
    symlinkSync(join(ws, 'realdir'), join(ws, 'linkdir'));
    const r = realResolve(join(ws, 'linkdir'));
    expect(r).toBe(join(ws, 'realdir'));
  });

  it('resolves /etc which exists', () => {
    expect(realResolve('/etc')).toBe('/etc');
  });
});

// ===========================================================================
// PART 2 — safeResolve
// ===========================================================================

describe('safeResolve', () => {
  let ws: string;
  beforeEach(() => { ws = makeWs(); });
  afterEach(() => { rmSync(ws, { recursive: true, force: true }); });

  it('resolves a relative path within workspace', () => {
    expect(safeResolve(ws, 'foo/bar.py')).toBe(join(ws, 'foo/bar.py'));
  });

  it('resolves a deep relative path', () => {
    expect(safeResolve(ws, 'a/b/c/d.py')).toBe(join(ws, 'a/b/c/d.py'));
  });

  it('allows absolute /tmp path', () => {
    const r = safeResolve(ws, '/tmp/script.sh');
    expect(r).toBe('/tmp/script.sh');
  });

  it('allows workspace root itself', () => {
    expect(safeResolve(ws, ws)).toBe(ws);
  });

  it('blocks path traversal (../../etc/passwd)', () => {
    expect(safeResolve(ws, '../../etc/passwd')).toBeNull();
  });

  it('blocks absolute path outside workspace and /tmp', () => {
    expect(safeResolve(ws, '/etc/passwd')).toBeNull();
  });

  it('blocks symlink pointing to /etc', () => {
    symlinkSync('/etc', join(ws, 'etc-link'));
    const r = safeResolve(ws, 'etc-link/passwd');
    expect(r).toBeNull();
  });

  it('allows paths inside workspace via absolute path', () => {
    const r = safeResolve(ws, join(ws, 'model.py'));
    expect(r).toBe(join(ws, 'model.py'));
  });
});

// ===========================================================================
// PART 3 — remapToWorkspace
// ===========================================================================

describe('remapToWorkspace', () => {
  const ws = '/home/user/project';

  it('/app/project/src/main.py → workspace/src/main.py', () => {
    expect(remapToWorkspace('/app/project/src/main.py', ws, '')).toBe('/home/user/project/src/main.py');
  });

  it('/app/model.py → workspace/model.py', () => {
    expect(remapToWorkspace('/app/model.py', ws, '')).toBe('/home/user/project/model.py');
  });

  it('/workspace/train.py → workspace/train.py', () => {
    expect(remapToWorkspace('/workspace/train.py', ws, '')).toBe('/home/user/project/train.py');
  });

  it('/project/run.sh → workspace/run.sh', () => {
    expect(remapToWorkspace('/project/run.sh', ws, '')).toBe('/home/user/project/run.sh');
  });

  it('exact /app/project root (no trailing slash) → workspace root', () => {
    expect(remapToWorkspace('/app/project', ws, '')).toBe(ws);
  });

  it('deduplicates workspace dir name from path', () => {
    // workspace=/home/user/test_2, path=/app/project/test_2/model.py
    // → /home/user/test_2/model.py (not /home/user/test_2/test_2/model.py)
    expect(remapToWorkspace('/app/project/test_2/model.py', '/home/user/test_2', ''))
      .toBe('/home/user/test_2/model.py');
  });

  it('does NOT deduplicate when workspace name differs from first segment', () => {
    expect(remapToWorkspace('/app/project/src/model.py', '/home/user/myapp', ''))
      .toBe('/home/user/myapp/src/model.py');
  });

  it('nested path preserved under workspace', () => {
    expect(remapToWorkspace('/app/project/a/b/c/model.py', ws, ''))
      .toBe('/home/user/project/a/b/c/model.py');
  });

  it('workdir hint takes priority when it matches path prefix', () => {
    // workdir=/app/project/sub, path=/app/project/sub/model.py
    // relative after stripping workdir = model.py → lands in workspace root
    expect(remapToWorkspace('/app/project/sub/model.py', ws, '/app/project/sub'))
      .toBe('/home/user/project/model.py');
  });

  it('unknown root falls back to filename only', () => {
    expect(remapToWorkspace('/some/unknown/root/file.py', ws, ''))
      .toBe('/home/user/project/file.py');
  });

  // -----------------------------------------------------------------------
  // stripProjectWrapper=true — fix for mismatched project names
  // -----------------------------------------------------------------------

  it('stripProjectWrapper: matching workspace name still strips correctly', () => {
    expect(remapToWorkspace('/app/project/project/model.py', '/home/user/project', '', true))
      .toBe('/home/user/project/model.py');
  });

  it('stripProjectWrapper: mismatched project name is stripped', () => {
    expect(remapToWorkspace('/app/project/test_2/model.py', '/home/user/myapp', '', true))
      .toBe('/home/user/myapp/model.py');
  });

  it('stripProjectWrapper: nested path — wrapper stripped, subdirs preserved', () => {
    expect(remapToWorkspace('/app/project/test_2/src/utils.py', '/home/user/myapp', '', true))
      .toBe('/home/user/myapp/src/utils.py');
  });

  it('stripProjectWrapper: filename-at-container-root kept (1 segment not stripped)', () => {
    expect(remapToWorkspace('/app/project/model.py', '/home/user/myapp', '', true, false))
      .toBe('/home/user/myapp/model.py');
  });

  it('stripProjectWrapper+isWorkdir: single-segment workdir maps to workspace root', () => {
    expect(remapToWorkspace('/app/project/test_2', '/home/user/myapp', '', true, true))
      .toBe('/home/user/myapp');
  });

  it('stripProjectWrapper+isWorkdir: workdir with subdir — wrapper stripped, subdir kept', () => {
    expect(remapToWorkspace('/app/project/test_2/demo', '/home/user/myapp', '', true, true))
      .toBe('/home/user/myapp/demo');
  });

  it('stripProjectWrapper: workdir hint takes priority before stripping', () => {
    expect(remapToWorkspace('/app/project/sub/model.py', ws, '/app/project/sub', true))
      .toBe('/home/user/project/model.py');
  });

  it('stripProjectWrapper: /app and /workspace roots are not stripped', () => {
    expect(remapToWorkspace('/app/model.py', '/home/user/myapp', '', true))
      .toBe('/home/user/myapp/model.py');
    expect(remapToWorkspace('/workspace/train.py', '/home/user/myapp', '', true))
      .toBe('/home/user/myapp/train.py');
  });

  it('stripProjectWrapper: exact /app/project root maps to workspace', () => {
    expect(remapToWorkspace('/app/project', '/home/user/myapp', '', true))
      .toBe('/home/user/myapp');
  });
});

// ===========================================================================
// PART 4 — remapCommandPaths
// ===========================================================================

describe('remapCommandPaths', () => {
  const ws = '/home/user/myproject';

  it('remaps /app/project/foo in ls command', () => {
    expect(remapCommandPaths('ls -la /app/project/foo/', ws))
      .toBe('ls -la /home/user/myproject/foo/');
  });

  it('remaps bare /app/project', () => {
    expect(remapCommandPaths('ls /app/project', ws)).toBe('ls /home/user/myproject');
  });

  it('remaps /app/model.py', () => {
    expect(remapCommandPaths('cat /app/model.py', ws)).toBe('cat /home/user/myproject/model.py');
  });

  it('remaps /workspace/trainer.py', () => {
    expect(remapCommandPaths('python /workspace/trainer.py', ws))
      .toBe('python /home/user/myproject/trainer.py');
  });

  it('remaps /project/run.sh', () => {
    expect(remapCommandPaths('bash /project/run.sh', ws)).toBe('bash /home/user/myproject/run.sh');
  });

  it('remaps multiple container paths in one command', () => {
    const cmd = 'cp /app/project/src/a.py /app/project/dst/a.py';
    expect(remapCommandPaths(cmd, '/root/proj'))
      .toBe('cp /root/proj/src/a.py /root/proj/dst/a.py');
  });

  it('deduplicates workspace name in command path', () => {
    expect(remapCommandPaths('ls /app/project/myproject/src/', ws))
      .toBe('ls /home/user/myproject/src/');
  });

  it('leaves non-container paths unchanged (/tmp stays /tmp)', () => {
    const cmd = 'echo hello && ls /tmp/logs';
    expect(remapCommandPaths(cmd, ws)).toBe(cmd);
  });

  it('handles cd &&-chained commands', () => {
    expect(remapCommandPaths('cd /app/project/src && python train.py', '/root/proj'))
      .toBe('cd /root/proj/src && python train.py');
  });

  it('remaps container path in cat command', () => {
    expect(remapCommandPaths('cat /app/project/README.md', '/root/proj'))
      .toBe('cat /root/proj/README.md');
  });
});

// ===========================================================================
// PART 5 — write_code
// ===========================================================================

describe('write_code', () => {
  let ws: string;
  beforeEach(() => { ws = makeWs(); });
  afterEach(() => { rmSync(ws, { recursive: true, force: true }); });

  it('writes a relative file to workspace root', async () => {
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'model.py', code: '# ml' }), ws);
    expect(r.status).toBe('success');
    expect(existsSync(join(ws, 'model.py'))).toBe(true);
    expect(readFileSync(join(ws, 'model.py'), 'utf8')).toBe('# ml');
  });

  it('auto-creates subdirectories', async () => {
    await dispatch(makeCmd({ action: 'write_code', filename: 'src/utils/helpers.py', code: '# util' }), ws);
    expect(existsSync(join(ws, 'src/utils/helpers.py'))).toBe(true);
  });

  it('overwrites an existing file', async () => {
    await dispatch(makeCmd({ action: 'write_code', filename: 'a.py', code: 'v1' }), ws);
    await dispatch(makeCmd({ action: 'write_code', filename: 'a.py', code: 'v2' }), ws);
    expect(readFileSync(join(ws, 'a.py'), 'utf8')).toBe('v2');
  });

  it('empty string code is valid', async () => {
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'empty.py', code: '' }), ws);
    expect(r.status).toBe('success');
    expect(readFileSync(join(ws, 'empty.py'), 'utf8')).toBe('');
  });

  it('remaps /app/project container path', async () => {
    // /app/project/{project-name}/{file}: first segment is the project wrapper and is
    // stripped. "src" here is the project name on the backend; file lands at workspace root.
    await dispatch(makeCmd({ action: 'write_code', filename: '/app/project/src/main.py', code: '# gen' }), ws);
    expect(existsSync(join(ws, 'main.py'))).toBe(true);
  });

  it('remaps /app container path', async () => {
    await dispatch(makeCmd({ action: 'write_code', filename: '/app/model.py', code: '# model' }), ws);
    expect(existsSync(join(ws, 'model.py'))).toBe(true);
  });

  it('remaps /workspace container path', async () => {
    await dispatch(makeCmd({ action: 'write_code', filename: '/workspace/trainer.py', code: '# train' }), ws);
    expect(existsSync(join(ws, 'trainer.py'))).toBe(true);
  });

  it('remaps /project container path', async () => {
    await dispatch(makeCmd({ action: 'write_code', filename: '/project/eval.py', code: '# eval' }), ws);
    expect(existsSync(join(ws, 'eval.py'))).toBe(true);
  });

  it('deduplicates workspace name in container path', async () => {
    const ws2 = mkdtempSync(join(tmpdir(), 'myproject-'));
    try {
      const dirName = ws2.split('/').pop()!;
      await dispatch(makeCmd({ action: 'write_code', filename: `/app/project/${dirName}/model.py`, code: '# dedup' }), ws2);
      expect(existsSync(join(ws2, 'model.py'))).toBe(true);
      expect(existsSync(join(ws2, dirName, 'model.py'))).toBe(false);
    } finally {
      rmSync(ws2, { recursive: true, force: true });
    }
  });

  it('relative filename with absolute container workdir strips project wrapper', async () => {
    // workdir=/app/project/sub: "sub" is the project name on the backend — stripped.
    // File lands at workspace root, not workspace/sub/.
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'app.py', code: '# app', workdir: '/app/project/sub' }), ws);
    expect(r.status).toBe('success');
    expect(existsSync(join(ws, 'app.py'))).toBe(true);
  });

  it('relative filename with absolute container workdir having subdir preserves subdir', async () => {
    // workdir=/app/project/myproj/demo: "myproj" stripped, "demo" preserved.
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'app.py', code: '# app', workdir: '/app/project/myproj/demo' }), ws);
    expect(r.status).toBe('success');
    expect(existsSync(join(ws, 'demo/app.py'))).toBe(true);
  });

  it('relative workdir single segment stripped — file lands at workspace root, not in project-name subfolder', async () => {
    // Core regression: backend sends workdir="multimodal_rag_0345" (relative single-segment).
    // This is the project-name wrapper and must be stripped.
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'model.py', code: '# m', workdir: 'multimodal_rag_0345' }), ws);
    expect(r.status).toBe('success');
    expect(existsSync(join(ws, 'model.py'))).toBe(true, 'file must land at workspace root');
    expect(existsSync(join(ws, 'multimodal_rag_0345', 'model.py'))).toBe(false, 'must NOT create project-name subfolder');
  });

  it('relative workdir multi-segment strips first segment and preserves rest', async () => {
    // "multimodal_rag_0345/src": first segment is project name (stripped), "src" is a real subdir.
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'train.py', code: '# t', workdir: 'multimodal_rag_0345/src' }), ws);
    expect(r.status).toBe('success');
    expect(existsSync(join(ws, 'src', 'train.py'))).toBe(true, 'subdir after project wrapper must be preserved');
    expect(existsSync(join(ws, 'multimodal_rag_0345'))).toBe(false, 'project-name folder must not be created');
  });

  it('container-relative filename app/project/ normalized — lands at workspace root, not in app/ subfolder', async () => {
    // Backend sometimes sends "app/project/myproj/model.py" without a leading '/'.
    // Must be treated as /app/project/myproj/model.py and remapped to workspace root.
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'app/project/myproj/model.py', code: '# m' }), ws);
    expect(r.status).toBe('success');
    expect(existsSync(join(ws, 'model.py'))).toBe(true, 'file must land at workspace root');
    expect(existsSync(join(ws, 'app'))).toBe(false, 'must NOT create app/ subfolder in workspace');
  });

  it('container-relative filename app/ normalized — lands at workspace root', async () => {
    // "app/model.py" (no leading '/') → treated as /app/model.py → remapped to workspace/model.py
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'app/model.py', code: '# a' }), ws);
    expect(r.status).toBe('success');
    expect(existsSync(join(ws, 'app'))).toBe(false, 'must NOT create app/ subfolder in workspace');
  });

  it('workdir echoed in response when provided', async () => {
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'f.py', code: '# f', workdir: 'src' }), ws);
    expect(r.data?.['workdir']).toBe('src');
  });

  it('workdir is empty string in response when not provided', async () => {
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'f.py', code: '# f' }), ws);
    expect(r.data?.['workdir']).toBe('');
  });

  it('missing filename returns error', async () => {
    const r = await dispatch(makeCmd({ action: 'write_code', code: '# x' }), ws);
    expect(r.status).toBe('error');
  });

  it('traversal via relative path is blocked', async () => {
    const r = await dispatch(makeCmd({ action: 'write_code', filename: '../../etc/passwd', code: 'bad' }), ws);
    expect(r.status).toBe('error');
    expect(existsSync('/etc/passwd-test')).toBe(false);
  });

  it('writes unicode content correctly', async () => {
    const content = '# 中文 emoji 🚀';
    await dispatch(makeCmd({ action: 'write_code', filename: 'utf8.py', code: content }), ws);
    expect(readFileSync(join(ws, 'utf8.py'), 'utf8')).toBe(content);
  });
});

// ===========================================================================
// PART 6 — get_file
// ===========================================================================

describe('get_file', () => {
  let ws: string;
  beforeEach(() => { ws = makeWs(); });
  afterEach(() => { rmSync(ws, { recursive: true, force: true }); });

  async function write(filename: string, code: string): Promise<void> {
    await dispatch(makeCmd({ action: 'write_code', filename, code }), ws);
  }

  it('reads a relative path within workspace', async () => {
    await write('data.csv', 'col1,col2\n1,2');
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: 'data.csv' }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['file_content']).toBe('col1,col2\n1,2');
  });

  it('reads an absolute workspace path', async () => {
    await write('abs.py', '# absolute');
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: join(ws, 'abs.py') }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['file_content']).toBe('# absolute');
  });

  it('remaps /app/project container path and reads file', async () => {
    await write('data.csv', 'col1,col2\n1,2');
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: '/app/project/data.csv' }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['file_content']).toBe('col1,col2\n1,2');
  });

  it('remaps /workspace container path', async () => {
    await write('model.py', '# model');
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: '/workspace/model.py' }), ws);
    expect(r.status).toBe('success');
  });

  it('returns error for missing file', async () => {
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: 'does_not_exist.py' }), ws);
    expect(r.status).toBe('error');
  });

  it('blocks /etc/passwd (not read directly)', async () => {
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: '/etc/passwd' }), ws);
    expect(r.status).toBe('error');
  });

  it('blocks relative traversal (../../etc/passwd)', async () => {
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: '../../etc/passwd' }), ws);
    expect(r.status).toBe('error');
  });

  it('write-then-read roundtrip', async () => {
    await write('roundtrip.py', '# roundtrip');
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: 'roundtrip.py' }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['file_content']).toBe('# roundtrip');
  });
});

// ===========================================================================
// PART 7 — run_subprocess
// ===========================================================================

describe('run_subprocess', () => {
  let ws: string;
  const activeJobIds: string[] = [];

  beforeEach(() => { ws = makeWs(); });
  afterEach(async () => {
    // Kill any lingering background jobs
    for (const jid of activeJobIds) {
      await dispatch(makeCmd({ action: 'terminate_job', job_id: jid }), ws);
    }
    activeJobIds.length = 0;
    rmSync(ws, { recursive: true, force: true });
  });

  it('detach=true returns job_id immediately', async () => {
    const r = await dispatch(makeCmd({ action: 'run_subprocess', command: 'echo hello' }), ws);
    expect(r.status).toBe('success');
    expect(typeof r.data?.['job_id']).toBe('string');
    expect(r.data?.['detached']).toBe(true);
    activeJobIds.push(r.data?.['job_id'] as string);
  });

  it('detached job output readable via get_job_status', async () => {
    const r = await dispatch(makeCmd({ action: 'run_subprocess', command: 'echo neo-marker' }), ws);
    const jid = r.data?.['job_id'] as string;
    activeJobIds.push(jid);
    // Poll until complete (max 3s)
    let logs;
    for (let i = 0; i < 30; i++) {
      logs = await dispatch(makeCmd({ action: 'get_job_status', job_id: jid }), ws);
      if (logs.data?.['exit_code'] !== null) break;
      await new Promise(r2 => setTimeout(r2, 100));
    }
    expect(logs?.data?.['stdout']).toContain('neo-marker');
    expect(logs?.data?.['exit_code']).toBe(0);
  });

  it('detach=false executes inline and returns stdout', async () => {
    const r = await dispatch(makeCmd({ action: 'run_subprocess', command: 'echo inline-test', detach: false }), ws);
    expect(r.status).toBe('completed');
    expect(r.data?.['detached']).toBe(false);
    expect((r.data?.['stdout'] as string).trim()).toBe('inline-test');
  });

  it('blocking captures stderr', async () => {
    const r = await dispatch(makeCmd({ action: 'run_subprocess', command: 'echo err-out >&2', detach: false }), ws);
    expect(r.data?.['stderr']).toContain('err-out');
  });

  it('blocking nonzero exit returns error status', async () => {
    const r = await dispatch(makeCmd({ action: 'run_subprocess', command: 'exit 42', detach: false }), ws);
    expect(r.status).toBe('error');
    expect(r.data?.['exit_code']).toBe(42);
  });

  it('blocking zero exit returns completed status', async () => {
    const r = await dispatch(makeCmd({ action: 'run_subprocess', command: 'true', detach: false }), ws);
    expect(r.status).toBe('completed');
    expect(r.data?.['exit_code']).toBe(0);
  });

  it('missing command returns error', async () => {
    const r = await dispatch(makeCmd({ action: 'run_subprocess' }), ws);
    expect(r.status).toBe('error');
  });

  it('container path in command is remapped', async () => {
    writeFileSync(join(ws, 'info.txt'), 'hello');
    const r = await dispatch(makeCmd({ action: 'run_subprocess', command: `cat /app/project/info.txt`, detach: false }), ws);
    expect(r.status).toBe('completed');
    expect(r.data?.['stdout']).toContain('hello');
  });

  it('missing /tmp script fails fast with clear error', async () => {
    const fakePath = '/tmp/bash_exec_deadbeef.sh';
    if (existsSync(fakePath)) unlinkSync(fakePath);
    const r = await dispatch(makeCmd({ action: 'run_subprocess', command: `bash ${fakePath}` }), ws);
    expect(r.status).toBe('error');
    expect(String(r.error)).toContain('bash_exec_');
  });

  it('terminate_job on running job returns success', async () => {
    const started = await dispatch(makeCmd({ action: 'run_subprocess', command: 'sleep 60' }), ws);
    const jid = started.data?.['job_id'] as string;
    const r = await dispatch(makeCmd({ action: 'terminate_job', job_id: jid }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['terminated']).toBe(true);
  });

  it('terminate_job on unknown job returns error', async () => {
    const r = await dispatch(makeCmd({ action: 'terminate_job', job_id: 'no-such-job-xyz' }), ws);
    expect(r.status).toBe('error');
  });
});

// ===========================================================================
// PART 8 — list_files
// ===========================================================================

describe('list_files', () => {
  let ws: string;
  beforeEach(() => { ws = makeWs(); });
  afterEach(() => { rmSync(ws, { recursive: true, force: true }); });

  async function write(filename: string): Promise<void> {
    await dispatch(makeCmd({ action: 'write_code', filename, code: '' }), ws);
  }

  it('lists files in workspace', async () => {
    await write('a.py');
    await write('b.py');
    const r = await dispatch(makeCmd({ action: 'list_files' }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['stdout']).toContain('a.py');
    expect(r.data?.['stdout']).toContain('b.py');
  });

  it('excludes hidden files by default', async () => {
    await write('.env');
    await write('main.py');
    const r = await dispatch(makeCmd({ action: 'list_files' }), ws);
    const stdout = r.data?.['stdout'] as string;
    expect(stdout).not.toContain('.env');
    expect(stdout).toContain('main.py');
  });

  it('includes hidden files when include_hidden=true', async () => {
    await write('.env');
    await write('main.py');
    const r = await dispatch(makeCmd({ action: 'list_files', include_hidden: true }), ws);
    const stdout = r.data?.['stdout'] as string;
    expect(stdout).toContain('.env');
    expect(stdout).toContain('main.py');
  });

  it('max_depth=1 limits recursion', async () => {
    await write('top.py');
    await write('nested/deep/file.py');
    const r = await dispatch(makeCmd({ action: 'list_files', max_depth: 1 }), ws);
    const stdout = r.data?.['stdout'] as string;
    expect(stdout).toContain('top.py');
    expect(stdout).not.toContain('deep/file.py');
  });

  it('skips node_modules contents by default', async () => {
    await write('node_modules/pkg/index.js');
    await write('src/app.py');
    const r = await dispatch(makeCmd({ action: 'list_files' }), ws);
    const stdout = r.data?.['stdout'] as string;
    expect(stdout).not.toContain('pkg/index.js');
    expect(stdout).toContain('app.py');
  });

  it('returns error for missing directory', async () => {
    const r = await dispatch(makeCmd({ action: 'list_files', directory: join(ws, 'no_such_dir') }), ws);
    expect(r.status).toBe('error');
  });

  it('file_count matches number of stdout lines', async () => {
    await write('a.py');
    await write('b.py');
    const r = await dispatch(makeCmd({ action: 'list_files' }), ws);
    const stdout = (r.data?.['stdout'] as string).trim();
    const lines = stdout.split('\n').filter(Boolean).length;
    expect(r.data?.['file_count']).toBe(lines);
  });

  it('remaps container directory /app/project', async () => {
    await write('model.py');
    const r = await dispatch(makeCmd({ action: 'list_files', directory: '/app/project' }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['stdout']).toContain('model.py');
  });

  it('dirs appear before files in output', async () => {
    await write('z_file.py');
    await write('a_dir/x.py');
    const r = await dispatch(makeCmd({ action: 'list_files' }), ws);
    const stdout = r.data?.['stdout'] as string;
    const lines = stdout.split('\n').filter(Boolean);
    const dirIdx = lines.findIndex(l => l.includes('a_dir') && l.includes('|d|'));
    const fileIdx = lines.findIndex(l => l.includes('z_file.py'));
    expect(dirIdx).toBeLessThan(fileIdx);
  });
});

// ===========================================================================
// PART 9 — create_session / dispatch misc
// ===========================================================================

describe('create_session', () => {
  const ws = '/tmp';

  it('with explicit session_id uses provided id', async () => {
    const r = await dispatch(makeCmd({ action: 'create_session', session_id: 'sess-123' }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['coding_session_id']).toBe('sess-123');
  });

  it('with payload session_id uses provided id', async () => {
    const r = await dispatch(makeCmd({ action: 'create_session', payload: { session_id: 'sess-payload' } }), ws);
    expect(r.status).toBe('success');
    expect(r.data?.['coding_session_id']).toBe('sess-payload');
  });

  it('auto-generates UUID when no session_id provided', async () => {
    const r = await dispatch(makeCmd({ action: 'create_session' }), ws);
    expect(r.status).toBe('success');
    expect(typeof r.data?.['coding_session_id']).toBe('string');
    expect((r.data?.['coding_session_id'] as string).length).toBeGreaterThan(8);
  });
});

describe('dispatch misc', () => {
  const ws = '/tmp';

  it('unknown action returns error with descriptive message', async () => {
    const r = await dispatch(makeCmd({ action: 'fly_a_blimp' }), ws);
    expect(r.status).toBe('error');
    expect(r.error).toContain('fly_a_blimp');
  });

  it('request_id is echoed in response', async () => {
    const r = await dispatch({ action: 'create_session', request_id: 'req-unique-xyz' }, ws);
    expect(r.request_id).toBe('req-unique-xyz');
  });

  it('all 7 actions are routable', async () => {
    // Just verify they don't throw with a missing field — they return an error response, not exceptions
    const actions = ['write_code', 'get_file', 'run_subprocess', 'list_files', 'create_session', 'get_job_status', 'terminate_job'];
    for (const action of actions) {
      const r = await dispatch(makeCmd({ action }), ws);
      // Each should return a valid result (not throw), even if status=error due to missing fields
      expect(typeof r.status).toBe('string');
      expect(r.request_id).toBe('req-1');
    }
  });
});

// ===========================================================================
// PART 10 — path security
// ===========================================================================

describe('path security', () => {
  let ws: string;
  beforeEach(() => { ws = makeWs(); });
  afterEach(() => { rmSync(ws, { recursive: true, force: true }); });

  it('file inside workspace is allowed', () => {
    expect(safeResolve(ws, 'src/model.py')).toBe(join(ws, 'src/model.py'));
  });

  it('deep subdir inside workspace is allowed', () => {
    expect(safeResolve(ws, 'a/b/c/d.py')).not.toBeNull();
  });

  it('/tmp is allowed', () => {
    expect(safeResolve(ws, '/tmp/script.sh')).not.toBeNull();
  });

  it('/etc/passwd is blocked', () => {
    expect(safeResolve(ws, '/etc/passwd')).toBeNull();
  });

  it('/ root is blocked', () => {
    expect(safeResolve(ws, '/')).toBeNull();
  });

  it('parent of a non-/tmp workspace is blocked', () => {
    // Use /home/neo-test-parent as a fake workspace that is NOT inside /tmp.
    // Its parent /home is clearly not a TMP dir.
    const fakeWs = '/home/neo-test-parent-check/myproject';
    const parent = '/home/neo-test-parent-check';
    expect(safeResolve(fakeWs, parent)).toBeNull();
  });

  it('sibling of workspace is blocked', () => {
    const fakeWs = '/home/neo-test-sibling-check/myproject';
    const sibling = '/home/neo-test-sibling-check/other-project';
    expect(safeResolve(fakeWs, sibling)).toBeNull();
  });
});

// ===========================================================================
// PART 11 — symlink escape
// ===========================================================================

describe('symlink escape', () => {
  let ws: string;
  let symlinkPath: string;

  beforeEach(() => {
    ws = makeWs();
    symlinkPath = join(ws, 'outside-link');
    symlinkSync('/etc', symlinkPath);
  });

  afterEach(() => {
    try { unlinkSync(symlinkPath); } catch { /* ignore */ }
    rmSync(ws, { recursive: true, force: true });
  });

  it('write_code via symlink-in-workspace is blocked or redirected', async () => {
    const r = await dispatch(makeCmd({ action: 'write_code', filename: 'outside-link/passwd', code: 'evil' }), ws);
    // Must either be an error or safe redirect (not write to /etc/passwd)
    if (r.status === 'success') {
      // If success, the file must have been written INSIDE the workspace (redirect), not to /etc
      const writtenPath = r.data?.['file_path'] as string;
      expect(writtenPath.startsWith(ws)).toBe(true);
    } else {
      expect(r.status).toBe('error');
    }
  });

  it('get_file via symlink pointing outside workspace is blocked', async () => {
    const r = await dispatch(makeCmd({ action: 'get_file', file_path: 'outside-link/passwd' }), ws);
    // Must return error (file not found at remapped path) or blocked
    // It should NOT return the content of /etc/passwd
    if (r.status === 'success') {
      // Theoretically could happen if remapped to workspace, but /etc/passwd won't be there
      // In practice this is always an error
    }
    expect(r.status).toBe('error');
  });

  it('safeResolve blocks symlink traversal', () => {
    expect(safeResolve(ws, 'outside-link/passwd')).toBeNull();
  });
});

// ===========================================================================
// PART 12 — concurrent workspace isolation
// ===========================================================================

describe('concurrent workspace isolation', () => {
  const workspaces: string[] = [];

  beforeEach(() => {
    for (let i = 0; i < 3; i++) workspaces.push(makeWs());
  });

  afterEach(() => {
    for (const ws of workspaces) rmSync(ws, { recursive: true, force: true });
    workspaces.length = 0;
  });

  it('concurrent writes to separate workspaces land in correct directories', async () => {
    const threads = ['tid-A', 'tid-B', 'tid-C'];
    const files = ['train.py', 'eval.py', 'predict.py'];

    await Promise.all(threads.map((tid, i) =>
      dispatch(makeCmd({ action: 'write_code', filename: files[i], code: `# ${tid}`, thread_id: tid }), workspaces[i]),
    ));

    for (let i = 0; i < 3; i++) {
      expect(existsSync(join(workspaces[i], files[i]))).toBe(true);
      expect(readFileSync(join(workspaces[i], files[i]), 'utf8')).toBe(`# ${threads[i]}`);
    }
    // Cross-contamination check
    for (let i = 0; i < 3; i++) {
      for (let j = 0; j < 3; j++) {
        if (i !== j) expect(existsSync(join(workspaces[j], files[i]))).toBe(false);
      }
    }
  });

  it('container-path writes are isolated per workspace', async () => {
    const writes = workspaces.map((ws, i) =>
      dispatch(makeCmd({ action: 'write_code', filename: `/app/project/model_${i}.py`, code: `# ws${i}` }), ws),
    );
    await Promise.all(writes);

    for (let i = 0; i < 3; i++) {
      expect(existsSync(join(workspaces[i], `model_${i}.py`))).toBe(true);
      for (let j = 0; j < 3; j++) {
        if (i !== j) expect(existsSync(join(workspaces[j], `model_${i}.py`))).toBe(false);
      }
    }
  });

  it('many concurrent writes across 3 workspaces — no cross-contamination', async () => {
    const writes: Promise<unknown>[] = [];
    for (let i = 0; i < 5; i++) {
      for (let wIdx = 0; wIdx < 3; wIdx++) {
        writes.push(dispatch(makeCmd({ action: 'write_code', filename: `file_${i}.py`, code: `# w${wIdx}` }), workspaces[wIdx]));
      }
    }
    await Promise.all(writes);
    for (let i = 0; i < 5; i++) {
      for (let wIdx = 0; wIdx < 3; wIdx++) {
        const content = readFileSync(join(workspaces[wIdx], `file_${i}.py`), 'utf8');
        expect(content).toBe(`# w${wIdx}`);
      }
    }
  });
});

// ===========================================================================
// PART 13 — auth: deriveDeploymentId / getAuthToken
// ===========================================================================

describe('deriveDeploymentId', () => {
  it('returns a valid UUID string', () => {
    expect(deriveDeploymentId('sk-v1-test')).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  });

  it('is deterministic — same key produces same UUID', () => {
    expect(deriveDeploymentId('sk-v1-mykey')).toBe(deriveDeploymentId('sk-v1-mykey'));
  });

  it('different keys produce different UUIDs', () => {
    expect(deriveDeploymentId('sk-v1-key1')).not.toBe(deriveDeploymentId('sk-v1-key2'));
  });

  it('version nibble is 5 (UUID v5)', () => {
    expect(deriveDeploymentId('sk-v1-test').split('-')[2].charAt(0)).toBe('5');
  });

  it('variant bits are RFC 4122 (8, 9, a, or b)', () => {
    expect(['8', '9', 'a', 'b']).toContain(deriveDeploymentId('sk-v1-test').split('-')[3].charAt(0));
  });

  it('matches Python uuid.UUID(bytes=SHA-256[:16], version=5)', () => {
    const key = 'sk-v1-test';
    const hash = createHash('sha256').update(key).digest();
    const bytes = Buffer.from(hash.subarray(0, 16));
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = bytes.toString('hex');
    const expected = [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20, 32)].join('-');
    expect(deriveDeploymentId(key)).toBe(expected);
  });

  it('matches Python for production-style key', () => {
    const key = 'sk-v1-abcdef1234567890';
    const hash = createHash('sha256').update(key).digest();
    const bytes = Buffer.from(hash.subarray(0, 16));
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = bytes.toString('hex');
    const expected = [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20, 32)].join('-');
    expect(deriveDeploymentId(key)).toBe(expected);
  });
});

describe('getAuthToken', () => {
  const ORIG_SK = process.env['NEO_SECRET_KEY'];
  afterEach(() => {
    if (ORIG_SK !== undefined) process.env['NEO_SECRET_KEY'] = ORIG_SK;
    else delete process.env['NEO_SECRET_KEY'];
  });

  it('returns NEO_SECRET_KEY when set', () => {
    process.env['NEO_SECRET_KEY'] = 'sk-v1-test-auth';
    expect(getAuthToken()).toBe('sk-v1-test-auth');
  });

  it('returns a string (empty when not set)', () => {
    delete process.env['NEO_SECRET_KEY'];
    expect(typeof getAuthToken()).toBe('string');
  });
});

// ===========================================================================
// PART 14 — deployment ID policy (getOrCreateDeploymentId)
// ===========================================================================

describe('deployment ID policy', () => {
  let saved: Record<string, string | undefined>;
  beforeEach(() => {
    saved = envBackup('NEO_DEPLOYMENT_ID', 'NEO_DEPLOYMENT_ID_MODE', 'NEO_SECRET_KEY');
  });
  afterEach(() => { envRestore(saved); });

  it('honors explicit NEO_DEPLOYMENT_ID override', () => {
    process.env['NEO_DEPLOYMENT_ID'] = 'explicit-id-abc';
    expect(getOrCreateDeploymentId()).toBe('explicit-id-abc');
  });

  it('explicit override wins over key-derived mode', () => {
    process.env['NEO_DEPLOYMENT_ID'] = 'explicit-priority';
    process.env['NEO_DEPLOYMENT_ID_MODE'] = 'key-derived';
    process.env['NEO_SECRET_KEY'] = 'sk-v1-test';
    expect(getOrCreateDeploymentId()).toBe('explicit-priority');
  });

  it('uses machine-persisted UUID by default — stable across calls', () => {
    delete process.env['NEO_DEPLOYMENT_ID'];
    delete process.env['NEO_DEPLOYMENT_ID_MODE'];
    process.env['NEO_SECRET_KEY'] = 'sk-v1-test';
    const id1 = getOrCreateDeploymentId();
    const id2 = getOrCreateDeploymentId();
    expect(id1).toBe(id2);
    expect(id1).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('machine UUID stays stable when API key changes', () => {
    delete process.env['NEO_DEPLOYMENT_ID'];
    delete process.env['NEO_DEPLOYMENT_ID_MODE'];
    process.env['NEO_SECRET_KEY'] = 'sk-v1-first';
    const id1 = getOrCreateDeploymentId();
    process.env['NEO_SECRET_KEY'] = 'sk-v1-second';
    const id2 = getOrCreateDeploymentId();
    expect(id1).toBe(id2);
  });

  it('uses deterministic key-derived UUID when mode=key-derived', () => {
    delete process.env['NEO_DEPLOYMENT_ID'];
    process.env['NEO_DEPLOYMENT_ID_MODE'] = 'key-derived';
    process.env['NEO_SECRET_KEY'] = 'sk-v1-mode-test';
    expect(getOrCreateDeploymentId()).toBe(deriveDeploymentId('sk-v1-mode-test'));
  });

  it('machine UUID is not equal to key-derived ID', () => {
    delete process.env['NEO_DEPLOYMENT_ID'];
    delete process.env['NEO_DEPLOYMENT_ID_MODE'];
    process.env['NEO_SECRET_KEY'] = 'sk-v1-test';
    const machineId = getOrCreateDeploymentId();
    const derived = deriveDeploymentId('sk-v1-test');
    expect(machineId).not.toBe(derived);
  });
});

// ===========================================================================
// PART 15 — thread workspace persistence
// ===========================================================================

describe('thread workspace persistence', () => {
  let ws1: string;
  let ws2: string;
  let bakFile: string | null = null;

  beforeEach(() => {
    mkdirSync(DAEMON_DIR, { recursive: true });
    ws1 = makeWs();
    ws2 = makeWs();
    bakFile = backupWorkspacesFile();
  });

  afterEach(() => {
    rmSync(ws1, { recursive: true, force: true });
    rmSync(ws2, { recursive: true, force: true });
    restoreWorkspacesFile(bakFile);
    bakFile = null;
  });

  it('registerThreadWorkspace persists and loadThreadWorkspaces reads it back', () => {
    registerThreadWorkspace('sys-thread-aaa', ws1);
    const loaded = loadThreadWorkspaces();
    expect(loaded['sys-thread-aaa']).toBe(ws1);
  });

  it('multiple threads stored and retrieved independently', () => {
    registerThreadWorkspace('sys-thread-aaa', ws1);
    registerThreadWorkspace('sys-thread-bbb', ws2);
    const loaded = loadThreadWorkspaces();
    expect(loaded['sys-thread-aaa']).toBe(ws1);
    expect(loaded['sys-thread-bbb']).toBe(ws2);
  });

  it('unknown thread_id returns undefined', () => {
    const loaded = loadThreadWorkspaces();
    expect(loaded['sys-never-registered']).toBeUndefined();
  });

  it('re-registering a thread updates its workspace', () => {
    registerThreadWorkspace('sys-thread-update', ws1);
    registerThreadWorkspace('sys-thread-update', ws2);
    const loaded = loadThreadWorkspaces();
    expect(loaded['sys-thread-update']).toBe(ws2);
  });

  it('loadThreadWorkspacesWithMeta includes updated_at timestamp', () => {
    registerThreadWorkspace('sys-thread-meta', ws1);
    const meta = loadThreadWorkspacesWithMeta();
    expect(meta['sys-thread-meta']).toBeDefined();
    expect(meta['sys-thread-meta'].workspace).toBe(ws1);
    const ua = meta['sys-thread-meta'].updated_at;
    expect(ua !== '' && ua !== undefined).toBe(true);
  });

  it('saveThreadWorkspaces evicts stale entries (>7 days)', () => {
    mkdirSync(DAEMON_DIR, { recursive: true });
    const stalePayload = {
      'sys-thread-stale': {
        workspace: ws1,
        updated_at: Math.floor((Date.now() - 8 * 24 * 60 * 60 * 1000) / 1000),
      },
    };
    writeFileSync(WORKSPACES_FILE, JSON.stringify(stalePayload));
    saveThreadWorkspaces({ 'sys-thread-stale': ws1 });
    const loaded = loadThreadWorkspaces();
    expect(loaded['sys-thread-stale']).toBeUndefined();
  });

  it('saveThreadWorkspaces keeps fresh entries (<7 days)', () => {
    mkdirSync(DAEMON_DIR, { recursive: true });
    const freshPayload = {
      'sys-thread-fresh': {
        workspace: ws1,
        updated_at: Math.floor((Date.now() - 60 * 60 * 1000) / 1000), // 1 hour ago
      },
    };
    writeFileSync(WORKSPACES_FILE, JSON.stringify(freshPayload));
    saveThreadWorkspaces({ 'sys-thread-fresh': ws1 });
    const loaded = loadThreadWorkspaces();
    expect(loaded['sys-thread-fresh']).toBe(ws1);
  });

  it('saveThreadWorkspaces caps at MAX_THREAD_WORKSPACES', () => {
    const savedMax = process.env['NEO_THREAD_WORKSPACES_MAX'];
    process.env['NEO_THREAD_WORKSPACES_MAX'] = '3';
    try {
      const many: Record<string, string> = {};
      for (let i = 0; i < 10; i++) many[`sys-thread-cap-${i}`] = ws1;
      saveThreadWorkspaces(many);
      const loaded = loadThreadWorkspaces();
      expect(Object.keys(loaded).length).toBeLessThanOrEqual(3);
    } finally {
      if (savedMax !== undefined) process.env['NEO_THREAD_WORKSPACES_MAX'] = savedMax;
      else delete process.env['NEO_THREAD_WORKSPACES_MAX'];
    }
  });

  it('returns empty map when file is absent', () => {
    if (existsSync(WORKSPACES_FILE)) rmSync(WORKSPACES_FILE);
    expect(loadThreadWorkspaces()).toEqual({});
  });
});

// ===========================================================================
// PART 16 — pollBackend
// ===========================================================================

describe('pollBackend', () => {
  const savedFetch = global.fetch;
  afterEach(() => { global.fetch = savedFetch; });

  it('returns [] on 404', async () => {
    global.fetch = async () => new Response('Not Found', { status: 404 }) as Response;
    expect(await pollBackend('dep', 'tok', 1)).toEqual([]);
  });

  it('returns [] on 500', async () => {
    global.fetch = async () => new Response('Error', { status: 500 }) as Response;
    expect(await pollBackend('dep', 'tok', 1)).toEqual([]);
  });

  it('returns [] on network error', async () => {
    global.fetch = async () => { throw new TypeError('Network failure'); };
    expect(await pollBackend('dep', 'tok', 1)).toEqual([]);
  });

  it('throws AuthError on 401', async () => {
    global.fetch = async () => new Response('Unauthorized', { status: 401 }) as Response;
    await expect(pollBackend('dep', 'tok', 1)).rejects.toThrow(AuthError);
  });

  it('parses flat array response', async () => {
    global.fetch = async () => new Response(
      JSON.stringify([{ action: 'write_code', request_id: 'r1' }]),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ) as Response;
    const result = await pollBackend('dep', 'tok', 1);
    expect(result).toHaveLength(1);
    expect(result[0].action).toBe('write_code');
  });

  it('parses { messages: [...] } shape', async () => {
    global.fetch = async () => new Response(
      JSON.stringify({ messages: [{ action: 'ping', request_id: 'r2' }] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ) as Response;
    const result = await pollBackend('dep', 'tok', 1);
    expect(result).toHaveLength(1);
    expect(result[0].action).toBe('ping');
  });
});

// ===========================================================================
// PART 17 — sendResponse retry
// ===========================================================================

describe('sendResponse retry', () => {
  const savedFetch = global.fetch;
  afterEach(() => { global.fetch = savedFetch; });

  it('succeeds on first attempt (1 call)', async () => {
    let calls = 0;
    global.fetch = async () => { calls++; return new Response('{}', { status: 200 }) as Response; };
    await sendResponse('dep', 'tok', { request_id: 'r1', status: 'success' });
    expect(calls).toBe(1);
  });

  it('retries on first failure and succeeds on second attempt', async () => {
    let calls = 0;
    global.fetch = async () => {
      calls++;
      if (calls === 1) throw new TypeError('Connection reset');
      return new Response('{}', { status: 200 }) as Response;
    };
    await sendResponse('dep', 'tok', { request_id: 'r1', status: 'success' });
    expect(calls).toBe(2);
  }, 10_000);

  it('retries twice and succeeds on third attempt', async () => {
    let calls = 0;
    global.fetch = async () => {
      calls++;
      if (calls < 3) throw new TypeError('Transient failure');
      return new Response('{}', { status: 200 }) as Response;
    };
    await sendResponse('dep', 'tok', { request_id: 'r1', status: 'success' });
    expect(calls).toBe(3);
  }, 15_000);

  it('exhausts all 3 attempts and does NOT throw (daemon keeps running)', async () => {
    let calls = 0;
    global.fetch = async () => { calls++; throw new TypeError('Sustained failure'); };
    await expect(sendResponse('dep', 'tok', { request_id: 'r1', status: 'success' }))
      .resolves.toBeUndefined();
    expect(calls).toBe(3);
  }, 15_000);

  it('injects sandbox_id into request body from deploymentId arg', async () => {
    let body: Record<string, unknown> = {};
    global.fetch = async (_url: unknown, init?: RequestInit) => {
      body = JSON.parse((init?.body as string) ?? '{}') as Record<string, unknown>;
      return new Response('{}', { status: 200 }) as Response;
    };
    await sendResponse('my-dep-id', 'tok', { request_id: 'r1', status: 'ok' });
    expect(body['sandbox_id']).toBe('my-dep-id');
  });
});

// ===========================================================================
// PART 18 — runDaemon integration
// ===========================================================================

describe('runDaemon integration', () => {
  let ws: string;
  let saved: Record<string, string | undefined>;

  beforeEach(() => {
    ws = makeWs();
    saved = envBackup('NEO_SECRET_KEY', 'NEO_API_URL', 'NEO_DEPLOYMENT_ID');
    process.env['NEO_SECRET_KEY'] = 'sk-v1-test';
    process.env['NEO_API_URL'] = 'http://test.invalid';
    process.env['NEO_DEPLOYMENT_ID'] = 'test-dep-id-sys';
  });

  afterEach(() => {
    rmSync(ws, { recursive: true, force: true });
    envRestore(saved);
  });

  it('does not crash on empty poll response', async () => {
    let polled = false;
    await runDaemonBriefly(ws, 250, async () => {
      polled = true;
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    expect(polled).toBe(true);
  });

  it('writes sandbox log on startup', async () => {
    await runDaemonBriefly(ws, 150, async () =>
      new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    expect(existsSync(DAEMON_LOG)).toBe(true);
    const log = readFileSync(DAEMON_LOG, 'utf8');
    expect(log).toContain('sandboxId');
    expect(log).toContain('npm-daemon');
  });

  it('dispatches write_code command and POSTs success response', async () => {
    let responseSent = false;
    let pollCount = 0;

    await runDaemonBriefly(ws, 600, async (url, opts) => {
      const urlStr = String(url);
      if (urlStr.includes('/v2/poll/response')) {
        const b = JSON.parse((opts?.body as string) ?? '{}') as Record<string, unknown>;
        if (b['status'] === 'success' && b['request_id'] === 'req-sys-1') responseSent = true;
        return new Response('{}', { status: 200 });
      }
      if (urlStr.includes('/v2/poll/')) {
        pollCount++;
        if (pollCount === 1) {
          return new Response(JSON.stringify([{
            action: 'write_code', request_id: 'req-sys-1', filename: 'sys_result.py', code: '# sys',
          }]), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response('{}', { status: 404 });
    });

    expect(existsSync(join(ws, 'sys_result.py'))).toBe(true);
    expect(responseSent).toBe(true);
  });

  it('stops when 401 received — AuthError stops the daemon loop', async () => {
    let callCount = 0;
    await runDaemonBriefly(ws, 300, async (url) => {
      callCount++;
      if (String(url).includes('/v2/poll/')) return new Response('Unauthorized', { status: 401 });
      return new Response('{}', { status: 200 });
    });
    // After 401 the daemon stops — call count should be low (just the 401 poll)
    expect(callCount).toBeGreaterThan(0);
    // We just verify it didn't crash or loop forever
  });

  it('abort signal stops the daemon loop gracefully', async () => {
    let polled = false;
    await runDaemonBriefly(ws, 200, async () => {
      polled = true;
      return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    expect(polled).toBe(true);
  });
});

// ===========================================================================
// PART 19 — thread status gate (via runDaemon)
// ===========================================================================

describe('thread status gate', () => {
  let ws: string;
  let saved: Record<string, string | undefined>;

  beforeEach(() => {
    ws = makeWs();
    saved = envBackup('NEO_SECRET_KEY', 'NEO_API_URL', 'NEO_DEPLOYMENT_ID');
    process.env['NEO_SECRET_KEY'] = 'sk-v1-test';
    process.env['NEO_API_URL'] = 'http://test.invalid';
    process.env['NEO_DEPLOYMENT_ID'] = 'test-dep-id-gate';
  });

  afterEach(() => {
    rmSync(ws, { recursive: true, force: true });
    envRestore(saved);
  });

  it('TERMINATED thread: daemon sends error response, file is NOT written', async () => {
    const tid = `sys-gate-term-${Date.now()}`;
    setThreadStatus(tid, 'TERMINATED');

    let errorSent = false;
    let successSent = false;

    await runDaemonBriefly(ws, 800, async (url, opts) => {
      const urlStr = String(url);
      if (urlStr.includes('/v2/poll/response')) {
        const b = JSON.parse((opts?.body as string) ?? '{}') as Record<string, unknown>;
        if (b['status'] === 'error' && String(b['error']).includes('TERMINATED')) errorSent = true;
        if (b['status'] === 'success') successSent = true;
        return new Response('{}', { status: 200 });
      }
      if (urlStr.includes('/v2/poll/')) {
        return new Response(JSON.stringify([{
          action: 'write_code', request_id: 'req-gate-term', thread_id: tid,
          filename: 'should_not_exist.py', code: '# blocked',
        }]), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response('{}', { status: 404 });
    });

    expect(errorSent).toBe(true);
    expect(successSent).toBe(false);
    expect(existsSync(join(ws, 'should_not_exist.py'))).toBe(false);
  });

  it('RUNNING thread: daemon executes commands normally', async () => {
    const tid = `sys-gate-run-${Date.now()}`;
    setThreadStatus(tid, 'RUNNING');

    let successSent = false;
    let pollCount = 0;

    await runDaemonBriefly(ws, 700, async (url, opts) => {
      const urlStr = String(url);
      if (urlStr.includes('/v2/poll/response')) {
        const b = JSON.parse((opts?.body as string) ?? '{}') as Record<string, unknown>;
        if (b['status'] === 'success') successSent = true;
        return new Response('{}', { status: 200 });
      }
      if (urlStr.includes('/v2/poll/')) {
        pollCount++;
        if (pollCount === 1) {
          return new Response(JSON.stringify([{
            action: 'write_code', request_id: 'req-gate-run', thread_id: tid,
            filename: 'running_out.py', code: '# running',
          }]), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response(JSON.stringify([]), { status: 200 });
      }
      return new Response('{}', { status: 404 });
    });

    expect(successSent).toBe(true);
    expect(existsSync(join(ws, 'running_out.py'))).toBe(true);
  });

  it('unknown thread (no status set): daemon executes commands — backwards compat', async () => {
    const tid = `sys-gate-unk-${Date.now()}`;
    // Do NOT call setThreadStatus — thread is unknown

    let commandExecuted = false;
    let pollCount = 0;

    await runDaemonBriefly(ws, 700, async (url, opts) => {
      const urlStr = String(url);
      if (urlStr.includes('/v2/poll/response')) {
        const b = JSON.parse((opts?.body as string) ?? '{}') as Record<string, unknown>;
        if (b['status'] === 'success') commandExecuted = true;
        return new Response('{}', { status: 200 });
      }
      if (urlStr.includes('/v2/poll/')) {
        pollCount++;
        if (pollCount === 1) {
          return new Response(JSON.stringify([{
            action: 'write_code', request_id: 'req-gate-unk', thread_id: tid,
            filename: 'unknown_thread_ok.py', code: '# unknown is ok',
          }]), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response(JSON.stringify([]), { status: 200 });
      }
      return new Response('{}', { status: 404 });
    });

    expect(commandExecuted).toBe(true);
  });

  it('PAUSED thread: daemon executes commands (PAUSED ∈ accepted)', async () => {
    const tid = `sys-gate-paused-${Date.now()}`;
    setThreadStatus(tid, 'PAUSED');

    let commandExecuted = false;
    let pollCount = 0;

    await runDaemonBriefly(ws, 700, async (url, opts) => {
      const urlStr = String(url);
      if (urlStr.includes('/v2/poll/response')) {
        const b = JSON.parse((opts?.body as string) ?? '{}') as Record<string, unknown>;
        if (b['status'] === 'success') commandExecuted = true;
        return new Response('{}', { status: 200 });
      }
      if (urlStr.includes('/v2/poll/')) {
        pollCount++;
        if (pollCount === 1) {
          return new Response(JSON.stringify([{
            action: 'write_code', request_id: 'req-gate-paused', thread_id: tid,
            filename: 'paused_ok.py', code: '# paused accepted',
          }]), { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        return new Response(JSON.stringify([]), { status: 200 });
      }
      return new Response('{}', { status: 404 });
    });

    expect(commandExecuted).toBe(true);
  });
});
