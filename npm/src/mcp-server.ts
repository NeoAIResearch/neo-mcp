/**
 * neo-mcp MCP server — stdio transport.
 *
 * Exposes 8 tools identical to the Python neo-mcp package:
 *   neo_submit_task, neo_task_status, neo_get_messages,
 *   neo_send_feedback, neo_pause_task, neo_resume_task, neo_stop_task,
 *   neo_list_tasks
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
  controlThread, getMessages, getTaskStatus,
  sendFeedback, stopThread, submitTask,
} from './neo-client';

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

  // Start daemon polling in the background so tasks actually execute locally.
  const abort = new AbortController();
  runDaemon({ workspace, deploymentId, signal: abort.signal }).catch(() => { /* exits on shutdown */ });

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const server: any = new McpServer({ name: 'neo-mcp-server', version: '1.1.20' });

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
        'Next: neo_task_status to poll, neo_get_messages when COMPLETED.',
      inputSchema: {
        message: z.string().describe(
          'Full task description. Be specific: state the goal, relevant file paths, and constraints. ' +
          'Example: "Train a sentiment classifier on data/reviews.csv, save to models/sentiment.pkl, target F1 > 0.85"',
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
        const result = await submitTask(token, deploymentId, message, effectiveWs);
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
        'Do NOT use to submit a new task — use neo_submit_task for that.',
      inputSchema: {
        thread_id: z.string().describe('Thread ID of the waiting task.'),
        message: z.string().describe(
          "Your reply to Neo's question, or additional instructions. " +
          'Example: "Yes, use PyTorch. Target accuracy is 90%."',
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
        await sendFeedback(token, thread_id, message);
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

  const transport = new StdioServerTransport();
  await server.connect(transport);

  // MCP server exited — shut down the background daemon cleanly.
  abort.abort();
}
