/**
 * Environment configuration.
 * NEO_ENV=staging  → https://alpha.heyneo.so
 * NEO_ENV=prod     → https://master.heyneo.so  (default)
 * NEO_API_URL      → explicit override (takes priority)
 */

const _env = (process.env['NEO_ENV'] ?? 'prod').toLowerCase();
const _defaultUrl = _env === 'staging' ? 'https://alpha.heyneo.so' : 'https://master.heyneo.so';

export const NEO_API_URL = process.env['NEO_API_URL'] ?? _defaultUrl;
export const NEO_ENV = _env;
