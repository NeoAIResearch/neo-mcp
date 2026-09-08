/**
 * Environment configuration.
 *
 * Precedence for the API base URL:
 *   1. ~/.neo/settings.json {"env": "staging"|"prod"}
 *   2. NEO_ENVIRONMENT or NEO_ENV ("staging"|"prod")
 *   3. NEO_API_URL (raw URL override)
 *   4. default → https://master.heyneo.com
 */

import { readFileSync } from 'fs';
import { SETTINGS_FILE } from './paths.js';

const PROD_URL = 'https://master.heyneo.com';
const STAGING_URL = 'https://alpha.heyneo.com';

function urlForEnv(value: string): string | null {
  const v = value.trim().toLowerCase();
  if (v === 'staging') return STAGING_URL;
  if (v === 'prod' || v === 'production') return PROD_URL;
  return null;
}

function envFromSettingsFile(path: string): string | null {
  try {
    const raw = readFileSync(path, 'utf-8');
    const data = JSON.parse(raw);
    if (data && typeof data === 'object' && typeof data.env === 'string') {
      return data.env;
    }
  } catch (e: unknown) {
    const err = e as NodeJS.ErrnoException;
    if (err && err.code !== 'ENOENT') {
      process.stderr.write(`[neo-mcp] Warning: could not parse ${path}: ${err.message ?? err}\n`);
    }
  }
  return null;
}

export function resolveApiUrl(opts?: { settingsFile?: string; env?: NodeJS.ProcessEnv }): { url: string; env: string } {
  const settingsFile = opts?.settingsFile ?? SETTINGS_FILE;
  const env = opts?.env ?? process.env;
  const settingsEnv = envFromSettingsFile(settingsFile);
  if (settingsEnv) {
    const u = urlForEnv(settingsEnv);
    if (u) return { url: u, env: settingsEnv.toLowerCase() };
  }
  const envVar = env['NEO_ENVIRONMENT'] ?? env['NEO_ENV'];
  if (envVar) {
    const u = urlForEnv(envVar);
    if (u) return { url: u, env: envVar.toLowerCase() };
  }
  const explicit = env['NEO_API_URL'];
  if (explicit) return { url: explicit, env: 'prod' };
  return { url: PROD_URL, env: 'prod' };
}

const _resolved = resolveApiUrl();
export const NEO_API_URL = _resolved.url;
export const NEO_ENV = _resolved.env;

// Poll parameters — mirrors Python config.py
export const POLL_MAX_MESSAGES = 20;            // commands fetched per poll
export const POLL_MAX_INTERVAL = 60_000;        // ms — cap for exponential backoff (60 s)
