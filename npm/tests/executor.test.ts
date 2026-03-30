import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { dispatch, safeResolve, Command } from '../src/executor.js';

// ---------------------------------------------------------------------------
// Test workspace
// ---------------------------------------------------------------------------

let workspace: string;

beforeEach(() => {
  workspace = mkdtempSync(join(tmpdir(), 'neo-daemon-test-'));
});

afterEach(() => {
  rmSync(workspace, { recursive: true, force: true });
});

function makeCmd(overrides: Partial<Command>): Command {
  return { action: 'noop', request_id: 'req-1', ...overrides };
}

// ---------------------------------------------------------------------------
// safeResolve
// ---------------------------------------------------------------------------

describe('safeResolve', () => {
  it('resolves relative paths within workspace', () => {
    const r = safeResolve(workspace, 'foo/bar.py');
    expect(r).toBe(join(workspace, 'foo/bar.py'));
  });

  it('allows /tmp', () => {
    const r = safeResolve(workspace, '/tmp/test.txt');
    expect(r).toBe('/tmp/test.txt');
  });

  it('blocks path traversal', () => {
    const r = safeResolve(workspace, '../../etc/passwd');
    expect(r).toBeNull();
  });

  it('blocks absolute paths outside workspace and /tmp', () => {
    const r = safeResolve(workspace, '/etc/passwd');
    expect(r).toBeNull();
  });

  it('allows paths deep inside workspace', () => {
    const r = safeResolve(workspace, 'a/b/c/d.txt');
    expect(r).toBe(join(workspace, 'a/b/c/d.txt'));
  });
});

// ---------------------------------------------------------------------------
// create_session
// ---------------------------------------------------------------------------

describe('dispatch: create_session', () => {
  it('returns a session ID', async () => {
    const result = await dispatch(makeCmd({ action: 'create_session' }), workspace);
    expect(result.status).toBe('success');
    expect(result.data?.['coding_session_id']).toBeTruthy();
  });

  it('echoes provided session_id', async () => {
    const result = await dispatch(makeCmd({ action: 'create_session', session_id: 'my-session' }), workspace);
    expect(result.data?.['coding_session_id']).toBe('my-session');
  });
});

// ---------------------------------------------------------------------------
// write_code
// ---------------------------------------------------------------------------

describe('dispatch: write_code', () => {
  it('writes a file into workspace', async () => {
    const result = await dispatch(makeCmd({
      action: 'write_code',
      filename: 'hello.py',
      code: 'print("hello")',
    }), workspace);
    expect(result.status).toBe('success');
    const content = readFileSync(join(workspace, 'hello.py'), 'utf8');
    expect(content).toBe('print("hello")');
  });

  it('creates subdirectories as needed', async () => {
    const result = await dispatch(makeCmd({
      action: 'write_code',
      filename: 'src/models/train.py',
      code: '# train',
    }), workspace);
    expect(result.status).toBe('success');
    expect(readFileSync(join(workspace, 'src/models/train.py'), 'utf8')).toBe('# train');
  });

  it('errors when filename missing', async () => {
    const result = await dispatch(makeCmd({ action: 'write_code', code: 'x' }), workspace);
    expect(result.status).toBe('error');
    expect(result.error).toMatch(/filename/);
  });

  it('blocks path traversal', async () => {
    const result = await dispatch(makeCmd({
      action: 'write_code',
      filename: '../../etc/evil.sh',
      code: 'rm -rf /',
    }), workspace);
    expect(result.status).toBe('error');
    expect(result.error).toMatch(/escapes/);
  });
});

// ---------------------------------------------------------------------------
// get_file
// ---------------------------------------------------------------------------

describe('dispatch: get_file', () => {
  it('reads a file written to workspace', async () => {
    // Write first
    await dispatch(makeCmd({ action: 'write_code', filename: 'data.txt', code: 'hello world' }), workspace);

    const result = await dispatch(makeCmd({ action: 'get_file', file_path: 'data.txt' }), workspace);
    expect(result.status).toBe('success');
    expect(result.data?.['file_content']).toBe('hello world');
  });

  it('errors when file does not exist', async () => {
    const result = await dispatch(makeCmd({ action: 'get_file', file_path: 'missing.txt' }), workspace);
    expect(result.status).toBe('error');
    expect(result.error).toMatch(/not found/i);
  });

  it('errors when file_path missing', async () => {
    const result = await dispatch(makeCmd({ action: 'get_file' }), workspace);
    expect(result.status).toBe('error');
    expect(result.error).toMatch(/required/);
  });
});

// ---------------------------------------------------------------------------
// run_subprocess
// ---------------------------------------------------------------------------

describe('dispatch: run_subprocess', () => {
  it('starts a background job and returns a job_id', async () => {
    const result = await dispatch(makeCmd({ action: 'run_subprocess', command: 'echo hi' }), workspace);
    expect(result.status).toBe('success');
    expect(result.data?.['job_id']).toBeTruthy();
    expect(result.data?.['detached']).toBe(true);
  });

  it('errors when command missing', async () => {
    const result = await dispatch(makeCmd({ action: 'run_subprocess' }), workspace);
    expect(result.status).toBe('error');
  });

  it('job completes and stdout is captured', async () => {
    const startResult = await dispatch(makeCmd({ action: 'run_subprocess', command: 'echo neo-test' }), workspace);
    const jobId = startResult.data?.['job_id'] as string;

    // Poll until done (max 3s)
    let statusResult = await dispatch(makeCmd({ action: 'get_job_status', job_id: jobId }), workspace);
    for (let i = 0; i < 30 && !statusResult.data?.['completed']; i++) {
      await new Promise(r => setTimeout(r, 100));
      statusResult = await dispatch(makeCmd({ action: 'get_job_status', job_id: jobId }), workspace);
    }
    expect(statusResult.data?.['completed']).toBe(true);
    expect((statusResult.data?.['stdout'] as string).trim()).toBe('neo-test');
    expect(statusResult.data?.['exit_code']).toBe(0);
  });

  it('supports non-detached execution when detach=false', async () => {
    const result = await dispatch(makeCmd({
      action: 'run_subprocess',
      command: 'echo inline-run',
      payload: { detach: false },
    }), workspace);
    expect(result.status).toBe('completed');
    expect(result.data?.['detached']).toBe(false);
    expect(result.data?.['completed']).toBe(true);
    expect((result.data?.['stdout'] as string).trim()).toBe('inline-run');
    expect(result.data?.['exit_code']).toBe(0);
  });

  it('reads command from payload when top-level command is missing', async () => {
    const result = await dispatch(makeCmd({
      action: 'run_subprocess',
      payload: { command: 'echo payload-cmd' },
    }), workspace);
    expect(result.status).toBe('success');
    expect(result.data?.['job_id']).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// get_job_status
// ---------------------------------------------------------------------------

describe('dispatch: get_job_status', () => {
  it('errors for unknown job_id', async () => {
    const result = await dispatch(makeCmd({ action: 'get_job_status', job_id: 'nonexistent' }), workspace);
    expect(result.status).toBe('error');
    expect(result.error).toMatch(/not found/i);
  });
});

// ---------------------------------------------------------------------------
// terminate_job
// ---------------------------------------------------------------------------

describe('dispatch: terminate_job', () => {
  it('terminates a running job', async () => {
    const startResult = await dispatch(makeCmd({ action: 'run_subprocess', command: 'sleep 60' }), workspace);
    const jobId = startResult.data?.['job_id'] as string;
    const termResult = await dispatch(makeCmd({ action: 'terminate_job', job_id: jobId }), workspace);
    expect(termResult.status).toBe('success');
    expect(termResult.data?.['terminated']).toBe(true);
  });

  it('errors for unknown job_id', async () => {
    const result = await dispatch(makeCmd({ action: 'terminate_job', job_id: 'no-such-job' }), workspace);
    expect(result.status).toBe('error');
  });
});

// ---------------------------------------------------------------------------
// list_files
// ---------------------------------------------------------------------------

describe('dispatch: list_files', () => {
  beforeEach(async () => {
    // Create test file tree
    mkdirSync(join(workspace, 'src'), { recursive: true });
    await dispatch(makeCmd({ action: 'write_code', filename: 'src/main.py', code: '# main' }), workspace);
    await dispatch(makeCmd({ action: 'write_code', filename: 'README.md', code: '# readme' }), workspace);
  });

  it('lists files in workspace', async () => {
    const result = await dispatch(makeCmd({ action: 'list_files' }), workspace);
    expect(result.status).toBe('success');
    const stdout = result.data?.['stdout'] as string;
    expect(stdout).toContain('main.py');
    expect(stdout).toContain('README.md');
  });

  it('includes directory markers', async () => {
    const result = await dispatch(makeCmd({ action: 'list_files' }), workspace);
    const stdout = result.data?.['stdout'] as string;
    const lines = stdout.split('\n');
    const dirLines = lines.filter(l => l.endsWith('|d|0'));
    expect(dirLines.length).toBeGreaterThan(0);
  });

  it('errors for non-existent directory', async () => {
    const result = await dispatch(makeCmd({ action: 'list_files', directory: '/nonexistent/path' }), workspace);
    expect(result.status).toBe('error');
  });

  it('skips hidden files by default', async () => {
    await dispatch(makeCmd({ action: 'write_code', filename: '.hidden', code: 'secret' }), workspace);
    const result = await dispatch(makeCmd({ action: 'list_files' }), workspace);
    const stdout = result.data?.['stdout'] as string;
    expect(stdout).not.toContain('.hidden');
  });
});

// ---------------------------------------------------------------------------
// Unknown action
// ---------------------------------------------------------------------------

describe('dispatch: unknown action', () => {
  it('returns error for unknown action', async () => {
    const result = await dispatch(makeCmd({ action: 'fly_to_moon' }), workspace);
    expect(result.status).toBe('error');
    expect(result.error).toMatch(/unknown action/i);
  });
});
