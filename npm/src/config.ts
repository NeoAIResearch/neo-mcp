/**
 * Environment configuration.
 * NEO_ENV=staging  → https://alpha.heyneo.so
 * NEO_ENV=prod     → https://master.heyneo.so  (default)
 * NEO_API_URL      → explicit override (takes priority)
 */

// Check NEO_ENVIRONMENT first, then NEO_ENV — mirrors Python config.py precedence.
const _env = (process.env['NEO_ENVIRONMENT'] ?? process.env['NEO_ENV'] ?? 'prod').toLowerCase();
const _defaultUrl = _env === 'staging' ? 'https://alpha.heyneo.so' : 'https://master.heyneo.so';

export const NEO_API_URL = process.env['NEO_API_URL'] ?? _defaultUrl;
export const NEO_ENV = _env;

// Poll parameters — mirrors Python config.py
export const POLL_MAX_MESSAGES = 20;            // commands fetched per poll
export const POLL_MAX_INTERVAL = 60_000;        // ms — cap for exponential backoff (60 s)
