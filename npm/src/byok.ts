/**
 * BYOK ("bring your own key") for the npm MCP server — parity with the Python
 * neo_mcp.byok package and the VS Code extension's src/llm/ modules.
 *
 * A profile is a named (provider, model) pair with an API key. Profile metadata
 * (id, name, provider, model) is persisted to ~/.neo/settings.json — alongside
 * the backend-env selection — while the key is written to
 * ~/.neo/integrations/byok-<id>.key at mode 0o600 (never into settings.json).
 *
 * When a profile is active, its credentials resolve to the three x-llm-* headers
 * that are attached to exactly init-chat-direct (submit) and feedback — the two
 * calls that drive Neo's orchestrator.
 */

import { randomUUID } from 'crypto';
import {
  existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync,
} from 'fs';
import { join } from 'path';
import { NEO_HOME, SETTINGS_FILE } from './paths.js';

export type LLMProvider = 'anthropic' | 'openai' | 'openrouter';
export const BYOK_PROVIDERS: LLMProvider[] = ['anthropic', 'openai', 'openrouter'];

export interface ByokProfile {
  id: string;
  name: string;
  provider: LLMProvider;
  model: string;
}

export type ByokHeaders = {
  'x-llm-key': string;
  'x-llm-provider': string;
  'x-llm-model': string;
};

const BYOK_DIR = join(NEO_HOME, 'integrations');
const PROFILES_KEY = 'byok_profiles';
const ACTIVE_KEY = 'active_byok_profile_id';

// Curated fallbacks, kept in sync with the extension's FALLBACK_MODELS.
export const FALLBACK_MODELS: Record<LLMProvider, string[]> = {
  anthropic: [
    'claude-opus-4-7', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001',
    'claude-opus-4-6', 'claude-sonnet-4-5-20250929', 'claude-opus-4-5-20251101',
    'claude-opus-4-1-20250805', 'claude-sonnet-4-20250514', 'claude-opus-4-20250514',
  ],
  openai: ['gpt-4o', 'gpt-4o-mini', 'o3', 'o3-mini', 'o4-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'],
  openrouter: [
    'anthropic/claude-opus-4.7', 'anthropic/claude-opus-4.6', 'anthropic/claude-sonnet-4.6',
    'openai/gpt-5.4', 'openai/gpt-5.4-mini', 'openai/gpt-5.4-nano', 'openai/gpt-5.4-pro',
    'openai/gpt-5.3-chat', 'openai/gpt-5.2',
  ],
};

export function isSupportedProvider(p: string): p is LLMProvider {
  return (BYOK_PROVIDERS as string[]).includes(p);
}

/**
 * Hyphenate dotted version numbers — Anthropic only (e.g. claude-opus-4.7 →
 * claude-opus-4-7). OpenAI and OpenRouter are left untouched: both have real
 * ids containing dots (gpt-4.1, anthropic/claude-opus-4.7) that hyphenation
 * would corrupt. Intentionally diverges from the extension's normalizeModelId,
 * which also hyphenates OpenAI (a latent bug for dotted OpenAI ids).
 */
export function normalizeModelId(provider: LLMProvider, modelId: string): string {
  if (provider === 'anthropic') return modelId.replace(/\./g, '-');
  return modelId;
}

function atomicWrite(path: string, content: string): void {
  const dir = join(path, '..');
  mkdirSync(dir, { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  writeFileSync(tmp, content, { mode: 0o600 });
  renameSync(tmp, path);
}

export class ByokManager {
  constructor(
    private readonly settingsFile: string = SETTINGS_FILE,
    private readonly byokDir: string = BYOK_DIR,
  ) {}

  // --- settings.json read / modify-write (preserves unrelated keys) -------

  private readSettings(): Record<string, unknown> {
    try {
      const data = JSON.parse(readFileSync(this.settingsFile, 'utf-8'));
      return data && typeof data === 'object' ? (data as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  }

  /**
   * Like readSettings but throws on a malformed (non-missing) file, so a
   * read-modify-write never silently clobbers a settings.json we couldn't
   * parse — that would wipe the user's `env` and any other keys.
   */
  private readSettingsStrict(): Record<string, unknown> {
    let raw: string;
    try {
      raw = readFileSync(this.settingsFile, 'utf-8');
    } catch (e) {
      if ((e as NodeJS.ErrnoException)?.code === 'ENOENT') return {};
      throw e;
    }
    let data: unknown;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      throw new Error(
        `Refusing to modify ${this.settingsFile}: it is not valid JSON ` +
        `(${e instanceof Error ? e.message : String(e)}). Fix or remove the file, then retry.`,
      );
    }
    if (!data || typeof data !== 'object') {
      throw new Error(`Refusing to modify ${this.settingsFile}: top-level value is not a JSON object.`);
    }
    return data as Record<string, unknown>;
  }

  private writeSettings(data: Record<string, unknown>): void {
    atomicWrite(this.settingsFile, `${JSON.stringify(data, null, 2)}\n`);
  }

  private mutateSettings(fn: (d: Record<string, unknown>) => void): void {
    const data = this.readSettingsStrict();
    fn(data);
    this.writeSettings(data);
  }

  // Same on-disk format as the Python FileStore (~/.neo/integrations/byok-<id>.env,
  // a `key=value` .env file) so a profile's key is interoperable between the
  // Python and npm servers, which share profile metadata via settings.json.
  private keyFile(id: string): string {
    return join(this.byokDir, `byok-${id}.env`);
  }

  // --- profile metadata ---------------------------------------------------

  listProfiles(): ByokProfile[] {
    const raw = this.readSettings()[PROFILES_KEY];
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((p): p is ByokProfile => !!p && typeof p === 'object' && 'id' in p)
      .map((p) => ({
        id: p.id,
        name: p.name ?? '',
        provider: p.provider,
        model: normalizeModelId(p.provider, p.model ?? ''),
      }));
  }

  getProfile(id: string): ByokProfile | null {
    return this.listProfiles().find((p) => p.id === id) ?? null;
  }

  getActiveProfileId(): string | null {
    const v = this.readSettings()[ACTIVE_KEY];
    return typeof v === 'string' ? v : null;
  }

  getActiveProfile(): ByokProfile | null {
    const id = this.getActiveProfileId();
    return id ? this.getProfile(id) : null;
  }

  // --- mutations ----------------------------------------------------------

  saveProfile(args: {
    name: string; provider: LLMProvider; model: string; apiKey: string;
    id?: string; setActive?: boolean;
  }): ByokProfile {
    if (!isSupportedProvider(args.provider)) {
      throw new Error(`Unsupported provider '${args.provider}'. Supported: ${BYOK_PROVIDERS.join(', ')}.`);
    }
    if (!args.name?.trim()) throw new Error('Profile name is required.');
    if (!args.model?.trim()) throw new Error('Model is required.');
    if (!args.apiKey?.trim()) throw new Error('API key is required.');

    const id = args.id ?? randomUUID();
    const saved: ByokProfile = {
      id,
      name: args.name.trim(),
      provider: args.provider,
      model: normalizeModelId(args.provider, args.model.trim()),
    };

    // Persist the key first so metadata never references a missing secret.
    atomicWrite(this.keyFile(id), `api_key=${args.apiKey.trim()}\n`);

    this.mutateSettings((d) => {
      const profiles = Array.isArray(d[PROFILES_KEY]) ? (d[PROFILES_KEY] as ByokProfile[]) : [];
      const idx = profiles.findIndex((p) => p?.id === id);
      if (idx >= 0) profiles[idx] = saved; else profiles.push(saved);
      d[PROFILES_KEY] = profiles;
      if (args.setActive) d[ACTIVE_KEY] = id;
    });
    return saved;
  }

  setActive(id: string | null): void {
    if (id !== null && !this.getProfile(id)) {
      throw new Error(`No BYOK profile with id '${id}'.`);
    }
    this.mutateSettings((d) => {
      if (id === null) delete d[ACTIVE_KEY];
      else d[ACTIVE_KEY] = id;
    });
  }

  deleteProfile(id: string): void {
    if (!id) throw new Error('profile_id is required.');
    this.mutateSettings((d) => {
      if (Array.isArray(d[PROFILES_KEY])) {
        d[PROFILES_KEY] = (d[PROFILES_KEY] as ByokProfile[]).filter((p) => p?.id !== id);
      }
      if (d[ACTIVE_KEY] === id) delete d[ACTIVE_KEY];
    });
    try { rmSync(this.keyFile(id), { force: true }); } catch { /* best effort */ }
  }

  // --- keys + header resolution ------------------------------------------

  getApiKey(id: string): string | null {
    const f = this.keyFile(id);
    if (!existsSync(f)) return null;
    try {
      // Parse the `api_key=...` line (Python FileStore .env format).
      for (const line of readFileSync(f, 'utf-8').split('\n')) {
        const t = line.trim();
        if (!t || t.startsWith('#')) continue;
        const eq = t.indexOf('=');
        if (eq < 0) continue;
        if (t.slice(0, eq).trim() === 'api_key') return t.slice(eq + 1).trim() || null;
      }
      return null;
    } catch {
      return null;
    }
  }

  keyHint(id: string): string {
    const k = this.getApiKey(id);
    if (!k) return '';
    return k.length <= 4 ? '••••' : `••••••••${k.slice(-4)}`;
  }

  resolveHeaders(profile: ByokProfile): ByokHeaders | null {
    const apiKey = this.getApiKey(profile.id);
    if (!apiKey) return null;
    return {
      'x-llm-key': apiKey,
      'x-llm-provider': profile.provider,
      'x-llm-model': profile.model,
    };
  }

  private envHeaders(): ByokHeaders | null {
    const key = (process.env['NEO_BYOK_KEY'] ?? '').trim();
    const provider = (process.env['NEO_BYOK_PROVIDER'] ?? '').trim().toLowerCase();
    const model = (process.env['NEO_BYOK_MODEL'] ?? '').trim();
    if (!key) return null;
    if (!isSupportedProvider(provider) || !model) return null;
    return {
      'x-llm-key': key,
      'x-llm-provider': provider,
      'x-llm-model': normalizeModelId(provider, model),
    };
  }

  /**
   * Resolve BYOK headers for an outgoing submit/feedback request.
   *  - { headers, error: null } → attach these headers.
   *  - { headers: null, error } → active profile has no key; surface the error.
   *  - { headers: null, error: null } → no BYOK; use Neo defaults.
   */
  resolveActiveHeaders(): { headers: ByokHeaders | null; error: string | null } {
    const profile = this.getActiveProfile();
    if (profile) {
      const headers = this.resolveHeaders(profile);
      if (!headers) {
        return {
          headers: null,
          error:
            `The active BYOK profile '${profile.name}' has no API key. ` +
            `Re-add it with neo_add_byok_profile, or clear it with ` +
            `neo_set_byok_profile (profile_id: null).`,
        };
      }
      return { headers, error: null };
    }
    return { headers: this.envHeaders(), error: null };
  }
}

// ---------------------------------------------------------------------------
// Credential testing — port of byokCredentialsTester.ts
// ---------------------------------------------------------------------------

const TEST_TIMEOUT_MS = 22_000;
const PROBE_MAX_TOKENS_LEGACY = 64;
const PROBE_MAX_COMPLETION_TOKENS = 128;
const INVALID_KEY_MSG =
  'Invalid API key — make sure you selected the correct provider and entered the right key.';

async function readErr(res: Response): Promise<string> {
  const text = await res.text();
  try {
    const j = JSON.parse(text) as { error?: { message?: string }; message?: string };
    const msg = j.error?.message ?? j.message;
    if (msg) return msg;
  } catch { /* ignore */ }
  return text.trim().slice(0, 280) || res.statusText || `HTTP ${res.status}`;
}

async function testOpenAICompatible(
  url: string, apiKey: string, model: string, extra: Record<string, string>, signal: AbortSignal,
): Promise<{ ok: boolean; message: string }> {
  const base = { model, messages: [{ role: 'user', content: 'ok' }] };
  const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}`, ...extra };
  let res = await fetch(url, {
    method: 'POST', signal, headers,
    body: JSON.stringify({ ...base, max_tokens: PROBE_MAX_TOKENS_LEGACY }),
  });
  if (res.ok) return { ok: true, message: '' };

  const err = await readErr(res);
  const lower = err.toLowerCase();
  const retry =
    lower.includes('max_completion_tokens') ||
    (lower.includes('max_tokens') && lower.includes('not supported')) ||
    lower.includes('max_tokens or model output limit') ||
    (lower.includes('output limit') && lower.includes('max_tokens')) ||
    (lower.includes('could not finish') && lower.includes('max_tokens'));
  if (retry) {
    res = await fetch(url, {
      method: 'POST', signal, headers,
      body: JSON.stringify({ ...base, max_completion_tokens: PROBE_MAX_COMPLETION_TOKENS }),
    });
    if (res.ok) return { ok: true, message: '' };
    return { ok: false, message: await readErr(res) };
  }
  if (res.status === 401) return { ok: false, message: `${INVALID_KEY_MSG} (${err})` };
  return { ok: false, message: err };
}

async function testAnthropic(
  model: string, apiKey: string, signal: AbortSignal,
): Promise<{ ok: boolean; message: string }> {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', 'x-api-key': apiKey, 'anthropic-version': '2023-06-01' },
    body: JSON.stringify({ model, max_tokens: 64, messages: [{ role: 'user', content: 'ok' }] }),
  });
  if (res.ok) return { ok: true, message: '' };
  const err = await readErr(res);
  if (res.status === 401) return { ok: false, message: `${INVALID_KEY_MSG} (${err})` };
  return { ok: false, message: err };
}

export async function testByokCredentials(
  provider: LLMProvider, model: string, apiKey: string,
): Promise<{ ok: boolean; message: string }> {
  const key = (apiKey ?? '').trim();
  if (!key) return { ok: false, message: 'API key is required.' };
  const m = normalizeModelId(provider, model);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TEST_TIMEOUT_MS);
  try {
    switch (provider) {
      case 'openai':
        return await testOpenAICompatible('https://api.openai.com/v1/chat/completions', key, m, {}, controller.signal);
      case 'openrouter':
        return await testOpenAICompatible(
          'https://openrouter.ai/api/v1/chat/completions', key, m,
          { 'HTTP-Referer': 'https://heyneo.so', 'X-Title': 'Neo MCP' }, controller.signal,
        );
      case 'anthropic':
        return await testAnthropic(m, key, controller.signal);
      default:
        return { ok: false, message: `Unknown provider '${provider}'.` };
    }
  } catch (e: unknown) {
    if (e instanceof Error && e.name === 'AbortError') {
      return { ok: false, message: 'Request timed out. Check your network and API reachability.' };
    }
    return { ok: false, message: e instanceof Error ? e.message : 'Request failed.' };
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Model fetching — port of modelFetcher.ts
// ---------------------------------------------------------------------------

const OPENAI_EXCLUDE_PREFIXES = [
  'text-embedding', 'text-moderation', 'text-search', 'text-similarity',
  'whisper', 'tts-', 'dall-e', 'omni-moderation',
  'babbage', 'davinci', 'curie', 'ada',
  'audio-', 'transcribe-', 'translate-', 'gpt-image', 'gpt-realtime', 'gpt-oss-',
];

export async function fetchModels(provider: LLMProvider, apiKey?: string): Promise<string[]> {
  try {
    if (provider === 'anthropic') return await fetchAnthropicModels(apiKey);
    if (provider === 'openai') return await fetchOpenAIModels(apiKey);
    if (provider === 'openrouter') return await fetchOpenRouterModels(apiKey);
  } catch { /* fall through */ }
  return FALLBACK_MODELS[provider] ?? [];
}

async function fetchAnthropicModels(apiKey?: string): Promise<string[]> {
  if (!apiKey) return FALLBACK_MODELS.anthropic;
  const models: string[] = [];
  let afterId: string | undefined;
  for (;;) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const url = new URL('https://api.anthropic.com/v1/models');
      url.searchParams.set('limit', '100');
      if (afterId) url.searchParams.set('after_id', afterId);
      const res = await fetch(url.toString(), {
        headers: { 'x-api-key': apiKey, 'anthropic-version': '2023-06-01' },
        signal: controller.signal,
      });
      if (!res.ok) break;
      const data = await res.json() as { data: { id: string }[]; has_more: boolean; last_id?: string };
      models.push(...data.data.map((d) => d.id));
      if (!data.has_more) break;
      afterId = data.last_id;
    } catch {
      break;
    } finally {
      clearTimeout(timer);
    }
  }
  return models.length ? models : FALLBACK_MODELS.anthropic;
}

async function fetchOpenAIModels(apiKey?: string): Promise<string[]> {
  if (!apiKey) return FALLBACK_MODELS.openai;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const res = await fetch('https://api.openai.com/v1/models', {
      headers: { Authorization: `Bearer ${apiKey}` }, signal: controller.signal,
    });
    if (!res.ok) return FALLBACK_MODELS.openai;
    const data = await res.json() as { data: { id: string }[] };
    const chat = data.data
      .map((d) => d.id)
      .filter((id) => !OPENAI_EXCLUDE_PREFIXES.some((p) => id.startsWith(p)))
      .filter((id) => !id.includes(':'))
      .sort().reverse();
    const merged = Array.from(new Set([...chat, ...FALLBACK_MODELS.openai]));
    return merged.length ? merged : FALLBACK_MODELS.openai;
  } catch {
    return FALLBACK_MODELS.openai;
  } finally {
    clearTimeout(timer);
  }
}

async function fetchOpenRouterModels(apiKey?: string): Promise<string[]> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  const headers: Record<string, string> = {};
  if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`;
  try {
    const res = await fetch('https://openrouter.ai/api/v1/models', { headers, signal: controller.signal });
    if (!res.ok) return FALLBACK_MODELS.openrouter;
    const data = await res.json() as { data: { id: string }[] };
    const models = data.data.map((d) => d.id).sort();
    return models.length ? models : FALLBACK_MODELS.openrouter;
  } catch {
    return FALLBACK_MODELS.openrouter;
  } finally {
    clearTimeout(timer);
  }
}
