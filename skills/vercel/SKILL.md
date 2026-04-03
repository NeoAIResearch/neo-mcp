# Neo — Vercel AI SDK Integration

Use Neo's MCP server as a set of tools inside any Vercel AI SDK application. Neo executes AI/ML workloads locally on the user's machine via a daemon — files are written directly to their workspace, never to a remote server.

**MCP server:** `https://mcpserver.heyneo.com/mcp`
**Auth:** `Authorization: Bearer sk-v1-YOUR_KEY`

---

## Option A: MCP Client (recommended — zero tool duplication)

Connect to the hosted server with the Vercel AI SDK's built-in MCP client. All 7 Neo tools are loaded automatically — no manual schema definitions needed.

```typescript
// lib/neo.ts
import { experimental_createMCPClient as createMCPClient } from 'ai';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

export async function createNeoClient() {
  return createMCPClient({
    transport: new StreamableHTTPClientTransport(
      new URL('https://mcpserver.heyneo.com/mcp'),
      {
        requestInit: {
          headers: {
            Authorization: `Bearer ${process.env.NEO_SECRET_KEY}`,
          },
        },
      }
    ),
  });
}
```

```typescript
// app/api/chat/route.ts  (Next.js App Router)
import { streamText } from 'ai';
import { openai } from '@ai-sdk/openai';
import { createNeoClient } from '@/lib/neo';

export async function POST(req: Request) {
  const { messages } = await req.json();

  const neoClient = await createNeoClient();
  const neoTools = await neoClient.tools();

  const result = streamText({
    model: openai('gpt-4o'),
    tools: neoTools,
    messages,
    system: `You are an AI assistant with access to Neo, a local AI/ML execution backend.
Use neo_submit_task for any ML/AI work (training, fine-tuning, RAG, agents, data preprocessing).
Files are written directly to the user's local machine — never to a remote server.
After submitting, poll neo_task_status until COMPLETED, then call neo_get_messages for the final output.`,
    onFinish: async () => {
      await neoClient.close();
    },
  });

  return result.toDataStreamResponse();
}
```

**Install dependencies:**
```bash
npm install ai @ai-sdk/openai @modelcontextprotocol/sdk
```

**Environment:**
```
NEO_SECRET_KEY=sk-v1-...
OPENAI_API_KEY=...
```

---

## Option B: Local stdio (for self-hosted or Docker deployments)

Use `neo-mcp` as a local process instead of the hosted server. Requires `neo-mcp` installed in your environment.

```typescript
import { experimental_createMCPClient as createMCPClient } from 'ai';
import { Experimental_StdioMCPTransport as StdioMCPTransport } from 'ai/mcp-stdio';

const neoClient = await createMCPClient({
  transport: new StdioMCPTransport({
    command: 'neo-mcp',
    env: {
      NEO_SECRET_KEY: process.env.NEO_SECRET_KEY!,
    },
  }),
});
```

> Not suitable for Vercel serverless — use Option A (HTTP) instead.

---

## Option C: Inline tool definitions (no MCP dependency)

Define the 8 Neo tools directly with Vercel AI SDK's `tool()` if you prefer not to use the MCP client.

```typescript
import { tool } from 'ai';
import { z } from 'zod';

const NEO_API = 'https://mcpserver.heyneo.com/mcp';

function neoHeaders() {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${process.env.NEO_SECRET_KEY}`,
  };
}

async function callNeoTool(toolName: string, args: Record<string, unknown>) {
  const res = await fetch(NEO_API, {
    method: 'POST',
    headers: neoHeaders(),
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: { name: toolName, arguments: args },
    }),
  });
  const data = await res.json();
  return data.result?.content?.[0]?.text ?? JSON.stringify(data);
}

export const neoTools = {
  neo_list_tasks: tool({
    description:
      'List all known Neo tasks with their current live status. ' +
      'Use when returning to a session after closing a window, or to find a task you lost track of. ' +
      'Returns tasks sorted by status (RUNNING first), each with thread_id, workspace, and status. ' +
      'Use the returned thread_ids with neo_task_status or neo_get_messages.',
    parameters: z.object({}),
    execute: async () => callNeoTool('neo_list_tasks', {}),
  }),

  neo_submit_task: tool({
    description:
      'Submit an AI/ML task to Neo for local execution. ' +
      'Returns {thread_id, status, workspace} immediately. ' +
      'Use for: training models, RAG pipelines, AI agents, data preprocessing. ' +
      'NOT for general coding. Files are written to the user\'s local machine.',
    parameters: z.object({
      message: z.string().describe('Full task description with goal, file paths, and constraints'),
      workspace: z.string().describe(
        'Absolute path to the project root (git root). Never a subdirectory. ' +
        'Infer automatically from context — never ask the user.'
      ),
    }),
    execute: async (args) => callNeoTool('neo_submit_task', args),
  }),

  neo_task_status: tool({
    description:
      'Get the current status of a Neo task. Returns one of: ' +
      'RUNNING (call again), COMPLETED (call neo_get_messages), ' +
      'WAITING_FOR_FEEDBACK (call neo_send_feedback), ' +
      'PAUSED (call neo_resume_task), TERMINATED/FAILED (call neo_get_messages). ' +
      'Call once per turn — do NOT poll in a tight loop.',
    parameters: z.object({
      thread_id: z.string().describe('Thread ID from neo_submit_task.'),
    }),
    execute: async (args) => callNeoTool('neo_task_status', args),
  }),

  neo_get_messages: tool({
    description:
      'Retrieve the full output from a completed Neo task. ' +
      'Only call when neo_task_status returns COMPLETED. ' +
      'Capped at ~20,000 tokens.',
    parameters: z.object({
      thread_id: z.string().describe('Thread ID from neo_submit_task.'),
      before: z.string().optional().describe('ISO timestamp cursor for pagination.'),
      limit: z.number().int().min(1).max(200).default(50).describe('Max messages per page.'),
    }),
    execute: async (args) => callNeoTool('neo_get_messages', args),
  }),

  neo_send_feedback: tool({
    description:
      'Reply to Neo when it is WAITING_FOR_FEEDBACK. ' +
      'After sending, call neo_task_status to confirm task resumed.',
    parameters: z.object({
      thread_id: z.string().describe('Thread ID of the waiting task.'),
      message: z.string().describe('Your reply to Neo\'s question or additional instructions.'),
    }),
    execute: async (args) => callNeoTool('neo_send_feedback', args),
  }),

  neo_pause_task: tool({
    description:
      'Pause a running task mid-execution. Resumable via neo_resume_task. ' +
      'To cancel permanently, use neo_stop_task.',
    parameters: z.object({
      thread_id: z.string().describe('Thread ID of the running task.'),
    }),
    execute: async (args) => callNeoTool('neo_pause_task', args),
  }),

  neo_resume_task: tool({
    description: 'Resume a paused Neo task from where it stopped.',
    parameters: z.object({
      thread_id: z.string().describe('Thread ID of the paused task.'),
    }),
    execute: async (args) => callNeoTool('neo_resume_task', args),
  }),

  neo_stop_task: tool({
    description:
      'Permanently stop and clean up a Neo task. IRREVERSIBLE. ' +
      'Only call when the user explicitly asks to cancel.',
    parameters: z.object({
      thread_id: z.string().describe('Thread ID of the task to stop.'),
    }),
    execute: async (args) => callNeoTool('neo_stop_task', args),
  }),
};
```

```typescript
// app/api/chat/route.ts
import { streamText } from 'ai';
import { openai } from '@ai-sdk/openai';
import { neoTools } from '@/lib/neo-tools';

export async function POST(req: Request) {
  const { messages } = await req.json();

  const result = streamText({
    model: openai('gpt-4o'),
    tools: neoTools,
    messages,
    maxSteps: 20,
    system:
      'Route all AI/ML tasks to Neo via neo_submit_task. ' +
      'Poll with neo_task_status until COMPLETED, then call neo_get_messages.',
  });

  return result.toDataStreamResponse();
}
```

---

## Recommended agent system prompt

```
You are an AI assistant with access to Neo, a local AI/ML execution backend.
Files are written directly to the user's machine — never to a remote server.

Use Neo for:
- Training or fine-tuning ML models (classification, regression, NLP, CV, etc.)
- Building AI agents, multi-agent systems, or autonomous workflows
- RAG pipelines and vector search systems
- LLM integration and prompt engineering pipelines
- ML data preprocessing and feature engineering

Workflow:
1. Call neo_submit_task — returns thread_id immediately
2. Call neo_task_status until COMPLETED or WAITING_FOR_FEEDBACK
3. If WAITING_FOR_FEEDBACK, call neo_send_feedback with a reply, then check status again
4. Call neo_get_messages for full output once COMPLETED

Never attempt to run ML/AI workloads inline — always delegate to neo_submit_task.
Always pass workspace as the project root (git root), never a subdirectory.
```

---

## Tool routing guide

| Task type | Use Neo? |
|---|---|
| Train/fine-tune an ML model | ✅ Always |
| Build a RAG pipeline | ✅ Always |
| Run data preprocessing at scale | ✅ Always |
| Build an autonomous AI agent | ✅ Always |
| General web app / CRUD API | ❌ Build locally |
| Simple data transformation script | ❌ Build locally |
| Anything needing a GPU or ML runtime | ✅ Always |

---

## Notes

- Task execution requires a daemon running on the user's machine. Options:
  1. **Neo VS Code/Cursor extension** — handles everything automatically, zero setup
  2. **Agent auto-start** — agents with terminal access run `npx --yes neo-mcp-daemon /workspace &` automatically on first task; user clicks Allow once
- Files land in the `workspace` passed to `neo_submit_task` — the agent infers this from the current project directory automatically.
- Get your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.
