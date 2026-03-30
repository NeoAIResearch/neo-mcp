# Neo — Vercel AI SDK Integration

Use Neo's MCP server as a set of tools inside any Vercel AI SDK application. Neo runs AI/ML workloads remotely so they don't block your serverless functions or local machine.

**MCP server:** `https://mcpserver.heyneo.com/mcp`
**Auth:** `Authorization: Bearer sk-v1-YOUR_KEY`

---

## Option A: MCP Client (recommended — zero tool duplication)

Connect to the hosted server with the Vercel AI SDK's built-in MCP client. All 9 Neo tools are loaded automatically — no manual schema definitions needed.

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
    system: `You are an AI assistant with access to Neo, a remote AI/ML execution backend.
Use neo_submit_task for any ML/AI work (training, fine-tuning, RAG, agents, data preprocessing).
After submitting, use neo_task_plan to check progress and neo_get_messages for the final output.`,
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

Define the Neo tools directly with Vercel AI SDK's `tool()` if you prefer not to use the MCP client.

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
  neo_submit_task: tool({
    description:
      'Submit an AI/ML task to Neo. Returns a thread_id immediately. ' +
      'Use wait_for_completion=true only for tasks under ~3 minutes.',
    parameters: z.object({
      description: z.string().describe('Full description of the task to run'),
      wait_for_completion: z
        .boolean()
        .optional()
        .describe('Block until done and return output directly (use for short tasks only)'),
      auto_mode: z
        .boolean()
        .optional()
        .describe('Run fully autonomously without pausing for feedback'),
    }),
    execute: async (args) => callNeoTool('neo_submit_task', args),
  }),

  neo_task_status: tool({
    description:
      'Check the current status of a Neo task. ' +
      'Returns RUNNING, COMPLETED, WAITING_FOR_FEEDBACK, PAUSED, or TERMINATED.',
    parameters: z.object({
      thread_id: z
        .string()
        .optional()
        .describe('Thread ID from neo_submit_task. Omit to use the last active thread.'),
    }),
    execute: async (args) => callNeoTool('neo_task_status', args),
  }),

  neo_task_plan: tool({
    description:
      'Show Neo\'s current execution plan with per-step status. ' +
      'Cheaper than neo_get_messages — use this while the task is RUNNING.',
    parameters: z.object({
      thread_id: z.string().optional().describe('Thread ID. Omit for last active thread.'),
    }),
    execute: async (args) => callNeoTool('neo_task_plan', args),
  }),

  neo_get_messages: tool({
    description: 'Get the full conversation output once a task is COMPLETED. Capped at ~20 000 tokens.',
    parameters: z.object({
      thread_id: z.string().optional().describe('Thread ID. Omit for last active thread.'),
    }),
    execute: async (args) => callNeoTool('neo_get_messages', args),
  }),

  neo_get_files: tool({
    description: 'Download files generated by a completed task (code, models, scripts). Returns contents inline.',
    parameters: z.object({
      thread_id: z.string().optional().describe('Thread ID. Omit for last active thread.'),
    }),
    execute: async (args) => callNeoTool('neo_get_files', args),
  }),

  neo_send_feedback: tool({
    description: 'Reply to Neo when it is WAITING_FOR_FEEDBACK.',
    parameters: z.object({
      message: z.string().describe('Your reply to Neo\'s question'),
      thread_id: z.string().optional().describe('Thread ID. Omit for last active thread.'),
    }),
    execute: async (args) => callNeoTool('neo_send_feedback', args),
  }),

  neo_stop_task: tool({
    description: 'Stop and clean up a running or paused task.',
    parameters: z.object({
      thread_id: z.string().optional().describe('Thread ID. Omit for last active thread.'),
      delete_remote_artifacts: z
        .boolean()
        .optional()
        .describe('Also delete files stored on Neo\'s servers'),
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
    system: 'Route all AI/ML tasks to Neo via neo_submit_task. ' +
            'Poll with neo_task_plan until COMPLETED, then call neo_get_messages.',
  });

  return result.toDataStreamResponse();
}
```

---

## Recommended agent system prompt

```
You are an AI assistant with access to Neo, a remote AI/ML execution backend.

Use Neo for:
- Training or fine-tuning ML models (classification, regression, NLP, CV, etc.)
- Building AI agents, multi-agent systems, or autonomous workflows
- RAG pipelines and vector search systems
- LLM integration and prompt engineering pipelines
- ML data preprocessing and feature engineering

Workflow:
1. Call neo_submit_task — returns thread_id immediately
2. Call neo_task_plan repeatedly until status is COMPLETED
3. If WAITING_FOR_FEEDBACK, call neo_send_feedback with a reply
4. Call neo_get_messages for full output
5. Call neo_get_files to retrieve any generated code or models

Never attempt to run ML/AI workloads inline — always delegate to neo_submit_task.
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

- `thread_id` is optional on all tools — the server auto-recovers the last active thread.
- `wait_for_completion: true` blocks until done. Only use for tasks under ~3 min.
- Task execution requires a daemon on the user's machine. Options (simplest first):
  1. **Neo VS Code/Cursor extension** — handles everything automatically, zero setup
  2. **Agent auto-start** — agents with terminal access offer to run `neo-mcp daemon &` on first task (user approves)
  3. **`neo-mcp login`** — runs browser OAuth and auto-starts daemon (needed until backend adds API key support to the poll endpoint)
- Once the backend poll endpoint accepts API keys, no login or manual daemon step is needed.
- Get your key at [app.heyneo.so](https://app.heyneo.so) → Settings → API Keys.
