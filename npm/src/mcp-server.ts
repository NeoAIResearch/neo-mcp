/**
 * neo-mcp MCP server — stdio transport.
 *
 * Exposes 13 tools identical to the Python neo-mcp package:
 *   neo_submit_task, neo_task_status, neo_get_messages,
 *   neo_send_feedback, neo_pause_task, neo_resume_task, neo_stop_task,
 *   neo_list_tasks,
 *   neo_list_byok_profiles, neo_add_byok_profile, neo_set_byok_profile,
 *   neo_remove_byok_profile, neo_list_byok_models
 *
 * Also starts the Neo daemon polling loop in the background so tasks
 * actually execute locally — mirrors the Python server's BackendPoller.
 *
 * Usage:
 *   NEO_SECRET_KEY=sk-v1-... npx neo-mcp-daemon --mcp [/path/to/workspace]
 *
 * Register with Claude Code:
 *   claude mcp add --scope user neo \
 *     -e NEO_SECRET_KEY=sk-v1-... \
 *     -- npx neo-mcp-daemon --mcp
 */

import { z } from 'zod';
import { getAuthToken } from './auth';
import { getOrCreateDeploymentId, loadThreadWorkspaces, loadThreadWorkspacesWithMeta, registerThreadWorkspace, runDaemon, setThreadStatus } from './daemon';
import {
  controlThread, fetchByokProviders, getMessages, getTaskStatus,
  sendFeedback, stopThread, submitTask,
} from './neo-client';
import {
  BYOK_PROVIDERS, ByokManager, fetchModels, isSupportedProvider,
  testByokCredentials, type LLMProvider,
} from './byok';
import { registerPostmanCapabilities } from './postman-capabilities.js';

// Return helpers.
// Note: explicit return-type annotations are intentionally omitted on handlers to avoid
// TS2589 "type instantiation excessively deep" — a known tsc limitation when McpServer
// infers through deeply-chained Zod generics. The returned shapes are correct at runtime.
function ok(data: unknown) {
  return { content: [{ type: 'text' as const, text: JSON.stringify(data, null, 2) }] };
}

function toolErr(e: unknown) {
  const msg = e instanceof Error ? e.message : String(e);
  return { content: [{ type: 'text' as const, text: `Error: ${msg}` }], isError: true as const };
}

export async function runMcpServer(opts: {
  workspace: string;
  deploymentId?: string;
}): Promise<void> {
  const token = getAuthToken();
  if (!token) {
    process.stderr.write(
      'ERROR: NEO_SECRET_KEY is not set.\n' +
      'Set your API key: export NEO_SECRET_KEY=sk-v1-...\n',
    );
    process.exit(1);
  }

  // Dynamic import lets CJS load the ESM-only @modelcontextprotocol/sdk (v1.6+).
  const { McpServer } = await import('@modelcontextprotocol/sdk/server/mcp');
  const { StdioServerTransport } = await import('@modelcontextprotocol/sdk/server/stdio');

  const deploymentId = opts.deploymentId ?? getOrCreateDeploymentId();
  const workspace = opts.workspace;
  const byok = new ByokManager();

  const BYOK_SAFETY =
    'Your LLM API key is stored only on this device (mode 0o600) — never written ' +
    'to settings.json. When this profile is active, the key is sent to Neo\'s backend ' +
    'ONLY as the x-llm-key header so Neo runs the orchestrator on your own LLM credits. ' +
    'Use a dedicated key with a spending limit. Remove it with neo_remove_byok_profile.';

  // Start daemon polling in the background so tasks actually execute locally.
  const abort = new AbortController();
  runDaemon({ workspace, deploymentId, signal: abort.signal }).catch(() => { /* exits on shutdown */ });

  // Version is read from package.json so it stays in sync across bumps —
  // mirrors _package_version() in the Python server.
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const _pkg = require('../package.json');
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const server: any = new McpServer({ name: 'neo-mcp-server', version: _pkg.version });

  // ----------------------------------------------------------------
  // neo_submit_task
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_submit_task',
    {
      title: 'Submit Neo Task',
      description:
        'Submit an AI/ML task to Neo for local execution. ' +
        'Use for: training/fine-tuning models, building AI agents, RAG pipelines, LLM integrations, ML data processing. ' +
        'NOT for general coding — write that code directly.\n\n' +
        'Execution is entirely local: the daemon runs on the user\'s machine and writes files directly to workspace. ' +
        'Files are never stored remotely. Neo\'s output may reference /app/project/src/model.py — the daemon ' +
        'automatically remaps this to <workspace>/src/model.py on the local disk.\n\n' +
        'Returns {thread_id, status, workspace} immediately. ' +
        'Next: neo_task_status to poll, neo_get_messages when COMPLETED.\n\n' +
        'Model and ID fidelity: if the user names a model, API, package, dataset, or other ' +
        'discrete ID, copy it verbatim into message. Do NOT substitute, upgrade, downgrade, ' +
        'shorten, or normalize to a different model (e.g. do not replace "gemini 3.1 pro" ' +
        'with gpt-4o, claude-sonnet, or an older variant). Do NOT call neo_list_byok_models ' +
        'or guess IDs when the user already specified one. When helpful, include in message: ' +
        '"Use exactly <user\'s id> — do not substitute a different model."',
      inputSchema: {
        message: z.string().describe(
          'Full task description. Be specific: state the goal, relevant file paths, and constraints. ' +
          'Example: "Train a sentiment classifier on data/reviews.csv, save to models/sentiment.pkl, target F1 > 0.85". ' +
          'If the user named a model or external ID, include the exact string they used — never a substitute or "equivalent" model.',
        ),
        workspace: z.string().describe(
          'Absolute path to the PROJECT ROOT (git repository root or top-level project folder). ' +
          'ALWAYS pass this automatically — never ask the user. ' +
          'NEVER use a subdirectory: if the user is inside /home/user/project/src, ' +
          'pass /home/user/project (the git root), not the src subfolder. ' +
          'Use git rev-parse --show-toplevel or process.cwd() for the project root. ' +
          'Wrong workspace causes files to land in the wrong place or creates duplicate nested folders.',
        ),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    async ({ message, workspace: ws }: { message: string; workspace: string }) => {
      try {
        const effectiveWs = ws || workspace;
        const { headers: byokHeaders, error: byokError } = byok.resolveActiveHeaders();
        if (byokError) return ok({ error: byokError });
        const result = await submitTask(token, deploymentId, message, effectiveWs, byokHeaders ?? undefined);
        // Register thread→workspace NOW — before any poll commands arrive.
        // The in-process daemon reloads thread-workspaces.json on first command
        // for a new thread_id, so writing here ensures it uses the right path.
        const threadId = result['thread_id'] as string | undefined;
        if (threadId) {
          // Mark RUNNING before any poll commands arrive — mirrors Python _submit_task.
          setThreadStatus(threadId, 'RUNNING');
          registerThreadWorkspace(threadId, effectiveWs);
        }
        // Normalize response to match Python _submit_task exactly:
        // return {"thread_id": thread_id, "status": "submitted", "workspace": ws}
        return ok({ thread_id: threadId, status: 'submitted', workspace: effectiveWs });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  // ----------------------------------------------------------------
  // neo_task_status
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_task_status',
    {
      title: 'Get Neo Task Status',
      description:
        'Get the current status of a Neo task. Returns one of:\n' +
        'RUNNING (still executing — call again; use neo_task_plan for step details),\n' +
        'COMPLETED (done — call neo_get_messages for output),\n' +
        'WAITING_FOR_FEEDBACK (Neo has a question — call neo_send_feedback),\n' +
        'PAUSED (frozen — call neo_resume_task to continue),\n' +
        'TERMINATED or FAILED (ended — call neo_get_messages to read what happened).\n\n' +
        'Reads from in-memory cache backed by an adaptive poller (3s–60s). ' +
        'Fast and safe to call once per turn. Do NOT poll in a tight loop.',
      inputSchema: {
        thread_id: z.string().describe('Thread ID from neo_submit_task. Example: "thread_abc123"'),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async ({ thread_id }: { thread_id: string }) => {
      try {
        return ok(await getTaskStatus(token, thread_id));
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  // ----------------------------------------------------------------
  // neo_get_messages
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_get_messages',
    {
      title: 'Get Neo Task Messages',
      description:
        'Retrieve the full conversation output from a completed Neo task. ' +
        'Only call when neo_task_status returns COMPLETED — for live progress while RUNNING ' +
        'use neo_task_plan instead (cheaper, shows per-step status).\n\n' +
        'Output is capped at ~80,000 characters (~20,000 tokens). If truncated, paginate ' +
        'backwards using the `before` cursor set to the ISO timestamp of the oldest message ' +
        'from the previous page.',
      inputSchema: {
        thread_id: z.string().describe('Thread ID returned by neo_submit_task.'),
        before: z.string().optional().describe(
          'Pagination cursor — ISO timestamp of the oldest message in the previous page. ' +
          'Omit to get the most recent messages.',
        ),
        limit: z.number().int().min(1).max(200).default(50).describe(
          'Maximum number of messages to return. Default: 50.',
        ),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async ({ thread_id, before, limit }: { thread_id: string; before?: string; limit: number }) => {
      try {
        return ok(await getMessages(token, thread_id, before, limit));
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  // ----------------------------------------------------------------
  // neo_send_feedback
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_send_feedback',
    {
      title: 'Send Neo Task Feedback',
      description:
        'Reply to Neo when it is waiting for user input. ' +
        'Only call when neo_task_status returns WAITING_FOR_FEEDBACK — Neo has paused and ' +
        'needs a decision or clarification before continuing. ' +
        'After sending, call neo_task_status again to confirm the task resumed.\n\n' +
        'Do NOT use to submit a new task — use neo_submit_task for that.\n\n' +
        'Model and ID fidelity: when correcting or clarifying which model or external ID to use, ' +
        "pass the user's exact wording in message. Do not override a user-specified model with a " +
        "default or 'better' alternative.",
      inputSchema: {
        thread_id: z.string().describe('Thread ID of the waiting task.'),
        message: z.string().describe(
          "Your reply to Neo's question, or additional instructions. " +
          'Example: "Yes, use PyTorch. Target accuracy is 90%." ' +
          "If clarifying a model or external ID, use the user's exact wording — never substitute a different model.",
        ),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    async ({ thread_id, message }: { thread_id: string; message: string }) => {
      try {
        const { headers: byokHeaders, error: byokError } = byok.resolveActiveHeaders();
        if (byokError) return ok({ error: byokError });
        await sendFeedback(token, thread_id, message, byokHeaders ?? undefined);
        return ok({ status: 'ok', thread_id });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  // ----------------------------------------------------------------
  // neo_pause_task
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_pause_task',
    {
      title: 'Pause Neo Task',
      description:
        'Pause a running Neo task mid-execution. The task freezes at its current step and ' +
        'can be resumed later with neo_resume_task. Safe to call on an already-paused task (no-op). ' +
        'To cancel permanently, use neo_stop_task instead.',
      inputSchema: {
        thread_id: z.string().describe('Thread ID of the running task to pause.'),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async ({ thread_id }: { thread_id: string }) => {
      try {
        await controlThread(token, thread_id, 'PAUSE');
        return ok({ status: 'paused', thread_id });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  // ----------------------------------------------------------------
  // neo_resume_task
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_resume_task',
    {
      title: 'Resume Neo Task',
      description:
        'Resume a paused Neo task from where it stopped. Has no effect if already running. ' +
        'Only works after neo_pause_task — to start a new task use neo_submit_task.',
      inputSchema: {
        thread_id: z.string().describe('Thread ID of the paused task to resume.'),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async ({ thread_id }: { thread_id: string }) => {
      try {
        await controlThread(token, thread_id, 'RESUME');
        return ok({ status: 'resumed', thread_id });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  // ----------------------------------------------------------------
  // neo_stop_task
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_stop_task',
    {
      title: 'Stop Neo Task',
      description:
        'Permanently stop and clean up a Neo task. ' +
        'IRREVERSIBLE — execution context is deleted and the task cannot be resumed. ' +
        'Only call when the user explicitly asks to cancel. ' +
        'To pause temporarily (resumable), use neo_pause_task instead.',
      inputSchema: {
        thread_id: z.string().describe('Thread ID of the task to stop and clean up.'),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    async ({ thread_id }: { thread_id: string }) => {
      try {
        await stopThread(token, thread_id);
        // Mark TERMINATED so the daemon rejects any in-flight commands for this thread —
        // mirrors Python _stop_task calling poller.set_thread_status(thread_id, 'TERMINATED').
        setThreadStatus(thread_id, 'TERMINATED');
        return ok({ status: 'stopped', thread_id });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  // ----------------------------------------------------------------
  // neo_list_tasks
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_list_tasks',
    {
      title: 'List Neo Tasks',
      description:
        'List all known Neo tasks with their current live status. ' +
        'Use this when returning to a session — e.g. after closing and reopening ' +
        'Claude Code — to see which tasks are still RUNNING, which are COMPLETED, ' +
        'and which need feedback. ' +
        'Returns tasks sorted newest-first. For each task: thread_id, workspace, ' +
        'status, and last-updated timestamp. ' +
        'After getting thread_ids, use neo_task_status or neo_get_messages to drill into a specific task.',
      inputSchema: {},
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async () => {
      try {
        const meta = loadThreadWorkspacesWithMeta();
        const entries = Object.entries(meta);
        if (entries.length === 0) return ok({ tasks: [], count: 0 });

        const tasks = await Promise.all(
          entries.map(async ([threadId, { workspace, updated_at }]) => {
            let status = 'UNKNOWN';
            try {
              const data = await getTaskStatus(token, threadId);
              status = (data['status'] as string) ?? 'UNKNOWN';
            } catch { /* unreachable thread — leave as UNKNOWN */ }
            return { thread_id: threadId, workspace, status, updated_at };
          }),
        );

        // Sort newest-first by updated_at — mirrors Python _list_tasks() sort.
        // updated_at may be an ISO string (written by Python daemon) or a Unix
        // timestamp in seconds (written by npm daemon); normalise to ms for comparison.
        function toMs(v: string | number): number {
          if (typeof v === 'number') return v * 1_000;
          const ms = Date.parse(v);
          return isNaN(ms) ? 0 : ms;
        }
        tasks.sort((a, b) => toMs(b.updated_at) - toMs(a.updated_at));

        return ok({ tasks, count: tasks.length });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  // ----------------------------------------------------------------
  // BYOK profile tools — run Neo's orchestrator on the user's own LLM key
  // ----------------------------------------------------------------
  server.registerTool(
    'neo_list_byok_profiles',
    {
      title: 'List BYOK Profiles',
      description:
        "List the configured BYOK ('bring your own key') LLM profiles and show which " +
        "one is active. A BYOK profile makes Neo run its orchestrator on the USER's own " +
        'LLM key (Anthropic / OpenAI / OpenRouter) instead of Neo\'s credits. Returns id, ' +
        'name, provider, model, a masked key hint, and the active flag. Never returns the raw key.',
      inputSchema: {},
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    },
    async () => {
      try {
        const activeId = byok.getActiveProfileId();
        const profiles = byok.listProfiles().map((p) => ({
          ...p, key_hint: byok.keyHint(p.id), active: p.id === activeId,
        }));
        return ok({ count: profiles.length, active_profile_id: activeId, profiles });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  server.registerTool(
    'neo_add_byok_profile',
    {
      title: 'Add BYOK Profile',
      description:
        "USE THIS TOOL when the user wants Neo to run on their own LLM key — phrasings like " +
        '"use my Anthropic key for Neo\'s brain", "run Neo on my own OpenAI key", "BYOK". ' +
        'This is DIFFERENT from neo_add_integration: integrations give a task subprocess ' +
        'credentials (env vars); a BYOK profile changes which LLM powers Neo\'s orchestrator ' +
        'itself (sent as the x-llm-* headers on submit + feedback). The key/model is validated ' +
        'against the provider before saving; an invalid key is rejected. By default the new ' +
        'profile becomes active. After it succeeds, relay the \'safety\' message verbatim and ' +
        'never echo the key.',
      inputSchema: {
        name: z.string().describe("Friendly profile name, e.g. 'My Claude Opus'."),
        provider: z.enum(['anthropic', 'openai', 'openrouter']).describe('LLM provider for the orchestrator.'),
        model: z.string().describe(
          "Model id valid for the provider's own API — pass the user's exact discrete ID verbatim. " +
          "Do NOT substitute example models like 'claude-opus-4-7' or 'gpt-4o' when the user named " +
          'something else. Call neo_list_byok_models only when the user did not specify a model; ' +
          'never pick the first list entry over an explicit user ID.',
        ),
        api_key: z.string().describe('The provider API key. Stored locally only.'),
        set_active: z.boolean().optional().describe('Make this the active profile (default true).'),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    },
    async ({ name, provider, model, api_key, set_active }: {
      name: string; provider: LLMProvider; model: string; api_key: string; set_active?: boolean;
    }) => {
      try {
        const setActive = set_active ?? true;
        const test = await testByokCredentials(provider, model, api_key);
        if (!test.ok) return ok({ status: 'rejected', ok: false, error: test.message });
        const saved = byok.saveProfile({ name, provider, model, apiKey: api_key, setActive });
        return ok({
          status: 'added', ok: true,
          profile: { ...saved, key_hint: byok.keyHint(saved.id) },
          active: setActive,
          safety: BYOK_SAFETY,
          assistant_instruction: "Relay the 'safety' message to the user verbatim. Never echo the key.",
        });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  server.registerTool(
    'neo_set_byok_profile',
    {
      title: 'Set Active BYOK Profile',
      description:
        'Select which BYOK profile is active, or clear it. Pass a profile_id (from ' +
        'neo_list_byok_profiles) to activate that profile, or null to deactivate BYOK so Neo ' +
        'uses its own default LLM credentials again. The active profile\'s key is attached to ' +
        'every subsequent neo_submit_task and neo_send_feedback.',
      inputSchema: {
        profile_id: z.string().nullable().describe('Profile id to activate, or null to clear.'),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    },
    async ({ profile_id }: { profile_id: string | null }) => {
      try {
        const id = (profile_id === '' || profile_id === 'null' || profile_id === 'none') ? null : profile_id;
        byok.setActive(id);
        return ok({ status: 'ok', active_profile_id: id, active_profile: byok.getActiveProfile() });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  server.registerTool(
    'neo_remove_byok_profile',
    {
      title: 'Remove BYOK Profile',
      description:
        'Delete a BYOK profile and its stored key. Irreversible — the user must re-add it via ' +
        'neo_add_byok_profile to use it again. If the deleted profile was active, BYOK is cleared ' +
        '(Neo falls back to its own LLM credentials).',
      inputSchema: {
        profile_id: z.string().describe('Profile id to delete (from neo_list_byok_profiles).'),
      },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false },
    },
    async ({ profile_id }: { profile_id: string }) => {
      try {
        byok.deleteProfile(profile_id);
        return ok({ status: 'removed', profile_id });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  server.registerTool(
    'neo_list_byok_models',
    {
      title: 'List BYOK Models',
      description:
        "List the model ids a BYOK provider supports. Prefers Neo's own catalog (the authority " +
        'on what task submit will accept); if that is unavailable, falls back to the provider\'s ' +
        'live model API (with an api_key) or a curated list. The response \'source\' field says ' +
        'which was used. Discovery only when the user did NOT name a model — never pick the first ' +
        'list entry over an explicit user ID.',
      inputSchema: {
        provider: z.enum(['anthropic', 'openai', 'openrouter']).describe('Provider to list models for.'),
        api_key: z.string().optional().describe('Optional API key to fetch the live model list.'),
      },
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
    },
    async ({ provider, api_key }: { provider: LLMProvider; api_key?: string }) => {
      try {
        if (!isSupportedProvider(provider)) {
          return ok({ error: `Unsupported provider '${provider}'. Supported: ${BYOK_PROVIDERS.join(', ')}.` });
        }
        // Prefer the Neo backend catalog (authority on submit-time acceptance);
        // fall back to the provider's own list if that call fails.
        let backendModels: string[] = [];
        try {
          const rows = await fetchByokProviders(token);
          backendModels = rows.find((r) => r.provider === provider)?.supported_models ?? [];
        } catch { /* fall back to provider list */ }
        if (backendModels.length) {
          return ok({ provider, source: 'neo-backend', count: backendModels.length, models: backendModels });
        }
        const models = await fetchModels(provider, api_key);
        return ok({ provider, source: 'provider', count: models.length, models });
      } catch (e) {
        return toolErr(e);
      }
    },
  );

  registerPostmanCapabilities(server);

  const transport = new StdioServerTransport();
  await server.connect(transport);

  // MCP server exited — shut down the background daemon cleanly.
  abort.abort();
}
