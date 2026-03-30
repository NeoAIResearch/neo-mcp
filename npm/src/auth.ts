import { createHash } from 'crypto';
import { readFileSync } from 'fs';
import { MCP_AUTH_FILE } from './paths.js';
import { NEO_API_URL } from './config.js';

interface McpAuth {
  access_token?: string;
  refresh_token?: string;
  username?: string;
}

/**
 * Derive a stable deployment UUID from an API key.
 *
 * Algorithm matches Python daemon exactly:
 *   SHA-256(key)[:16] formatted as UUID with RFC 4122 version=5, variant=1.
 * Same key → same UUID on every call, across Python and Node daemons.
 */
export function deriveDeploymentId(secretKey: string): string {
  const hash = createHash('sha256').update(secretKey).digest();
  const bytes = Buffer.from(hash.subarray(0, 16));

  // Apply RFC 4122 version 5 and variant bits — mirrors Python's uuid.UUID(bytes=..., version=5)
  bytes[6] = (bytes[6] & 0x0f) | 0x50; // version 5 (0101xxxx)
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 1 (10xxxxxx)

  const hex = bytes.toString('hex');
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join('-');
}

export function loadMcpAuth(): McpAuth {
  try {
    return JSON.parse(readFileSync(MCP_AUTH_FILE, 'utf8')) as McpAuth;
  } catch {
    return {};
  }
}

/**
 * Return the best available auth token.
 * Priority: OAuth access_token from mcp_auth.json → NEO_SECRET_KEY env var.
 */
export function getAuthToken(): string {
  const auth = loadMcpAuth();
  const token = auth.access_token ?? '';
  const invalid = ['', '\\', 'null', 'undefined'];
  if (!invalid.includes(token) && token.length >= 10) {
    return token;
  }
  return process.env['NEO_SECRET_KEY'] ?? '';
}

export function saveMcpAuth(data: McpAuth): void {
  const { writeFileSync, mkdirSync } = require('fs') as typeof import('fs');
  const { dirname } = require('path') as typeof import('path');
  mkdirSync(dirname(MCP_AUTH_FILE), { recursive: true });
  writeFileSync(MCP_AUTH_FILE, JSON.stringify(data, null, 2), { mode: 0o600 });
}

export async function refreshAuthToken(): Promise<string> {
  const auth = loadMcpAuth();
  const { refresh_token, username } = auth;
  if (!refresh_token || !username) return '';

  // Auth refresh uses the same base URL as the API
  const authUrl = process.env['NEO_AUTH_URL'] ?? NEO_API_URL;
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10_000);
    const res = await fetch(`${authUrl}/auth/refresh-token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, refreshToken: refresh_token }),
      signal: controller.signal,
    });
    clearTimeout(timeout);

    if (res.ok) {
      const data = await res.json() as Record<string, string>;
      const newToken = data['token'] ?? data['access_token'] ?? data['accessToken'] ?? '';
      if (newToken) {
        saveMcpAuth({ ...auth, access_token: newToken });
        return newToken;
      }
    }
  } catch {
    // network error or abort — fall through
  }
  return '';
}
