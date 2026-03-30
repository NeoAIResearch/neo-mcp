import { createHash } from 'crypto';

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

/** Return the NEO_SECRET_KEY API key used for all requests. */
export function getAuthToken(): string {
  return process.env['NEO_SECRET_KEY'] ?? '';
}
