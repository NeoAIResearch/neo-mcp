/**
 * Daemon logger matching the VS Code extension's DaemonLogger format.
 *
 * The extension writes runtime logs to ~/.neo/daemon/daemon.log as:
 *   [<ISO timestamp>] [<LEVEL>] <message> <meta-json>\n
 *
 * Pip and npm both write their own runtime log to ~/.neo/daemon/neo-mcp.log
 * using the same format and rotation policy so the same parser can consume
 * any of the three.
 *
 * Birth-time portability: the extension uses `birthtimeMs`, which on Linux
 * Node is unreliable on filesystems without statx birth-time support. We use
 * a sidecar file `<logfile>.birth` containing the Unix epoch timestamp at
 * first creation (rewritten on every rotation).
 */

import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'fs';
import { dirname } from 'path';

export const ROTATION_AGE_MS = 12 * 60 * 60 * 1000;        // 12 h, matches Logger.ts
export const ROTATION_CHECK_INTERVAL_MS = 60 * 60 * 1000;  // 1 h
export const MAX_ROTATED = 4;

export interface LogMeta {
  [key: string]: unknown;
}

type Level = 'INFO' | 'WARN' | 'ERROR' | 'DEBUG';

export class DaemonLogger {
  private readonly birthFile: string;
  private rotationTimer: NodeJS.Timeout | null = null;

  constructor(
    private readonly logFile: string,
    private readonly deploymentId: string,
    private readonly suppressConsole: boolean = process.env['NEO_SUPPRESS_CONSOLE'] === 'true',
    private readonly rotationAgeMs: number = ROTATION_AGE_MS,
    private readonly maxRotated: number = MAX_ROTATED,
  ) {
    this.birthFile = `${logFile}.birth`;
    try {
      mkdirSync(dirname(logFile), { recursive: true, mode: 0o700 });
    } catch { /* permission errors handled lazily on write */ }
    this.ensureBirthRecorded();
    this.rotateIfNeeded();
    this.startRotationTimer();
  }

  private ensureBirthRecorded(): void {
    try {
      if (!existsSync(this.logFile)) {
        writeFileSync(this.birthFile, String(Date.now()));
        return;
      }
      if (!existsSync(this.birthFile)) {
        // Pre-existing log without sidecar (upgrade case) — backfill with mtime.
        const mtimeMs = statSync(this.logFile).mtimeMs;
        writeFileSync(this.birthFile, String(mtimeMs));
      }
    } catch { /* read-only / permission — skip rotation tracking */ }
  }

  private fileAgeMs(): number {
    try {
      const birth = parseFloat(readFileSync(this.birthFile, 'utf-8').trim());
      if (!Number.isFinite(birth)) return 0;
      return Math.max(0, Date.now() - birth);
    } catch {
      return 0;
    }
  }

  private rotateIfNeeded(): void {
    try {
      if (!existsSync(this.logFile)) return;
      if (this.fileAgeMs() < this.rotationAgeMs) return;

      const oldest = `${this.logFile}.${this.maxRotated}`;
      if (existsSync(oldest)) unlinkSync(oldest);
      for (let i = this.maxRotated - 1; i >= 1; i--) {
        const from = `${this.logFile}.${i}`;
        const to = `${this.logFile}.${i + 1}`;
        if (existsSync(from)) renameSync(from, to);
      }
      renameSync(this.logFile, `${this.logFile}.1`);
      writeFileSync(this.birthFile, String(Date.now()));
    } catch { /* rotation must never crash the daemon */ }
  }

  private startRotationTimer(): void {
    this.rotationTimer = setInterval(() => this.rotateIfNeeded(), ROTATION_CHECK_INTERVAL_MS);
    this.rotationTimer.unref();
  }

  private write(level: Level, message: string, meta?: LogMeta): void {
    const ts = new Date().toISOString();
    const fullMeta: LogMeta = { deploymentId: this.deploymentId, ...(meta ?? {}) };
    const line = `[${ts}] [${level}] ${message} ${JSON.stringify(fullMeta)}\n`;

    try {
      this.rotateIfNeeded();
      appendFileSync(this.logFile, line);
    } catch { /* best-effort — don't crash on disk full / permission */ }

    if (this.suppressConsole) return;

    // Mirror to stderr so `claude mcp logs neo` users still see live output.
    // Wrap in try/catch — EPIPE is expected when the parent process closes.
    try {
      const out = level === 'ERROR' ? console.error : level === 'WARN' ? console.warn : console.error;
      out(line.trimEnd());
    } catch { /* EPIPE on shutdown */ }
  }

  info(message: string, meta?: LogMeta): void { this.write('INFO', message, meta); }
  warn(message: string, meta?: LogMeta): void { this.write('WARN', message, meta); }
  error(message: string, meta?: LogMeta): void { this.write('ERROR', message, meta); }
  debug(message: string, meta?: LogMeta): void { this.write('DEBUG', message, meta); }

  close(): void {
    if (this.rotationTimer) {
      clearInterval(this.rotationTimer);
      this.rotationTimer = null;
    }
  }
}

/** Convenience constructor — used by daemon.ts and mcp-server.ts. */
export function createDaemonLogger(logFile: string, deploymentId: string): DaemonLogger {
  return new DaemonLogger(logFile, deploymentId);
}
