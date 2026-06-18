/**
 * MCP prompts and resources for Postman and other MCP clients.
 */

import { z } from 'zod';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type McpServerLike = any;

const optPath = (args: { path?: string }, fallback: string) => args.path || fallback;

const RESOURCE_DOCS: Record<string, { title: string; body: string }> = {
  'neo://docs/overview': {
    title: 'Neo MCP Overview',
    body: `# Neo MCP Overview

Neo MCP connects Neo — an autonomous AI engineer for ML, LLM, GenAI, and data workflows — to MCP clients like Postman.

## AI engineering use cases

- RAG and semantic search
- LLM fine-tuning and prompt benchmarking
- AI agents with tools
- Classical ML, computer vision, EDA

Links: https://heyneo.com · https://docs.heyneo.com/neo-mcp
`,
  },
  'neo://docs/tools': {
    title: 'Tool Reference',
    body: `# Neo MCP Tools

| Tool | Description |
|------|-------------|
| neo_submit_task | Submit a task; returns thread_id |
| neo_list_tasks | List running and recent tasks |
| neo_task_status | Check RUNNING / COMPLETED / WAITING_FOR_FEEDBACK |
| neo_get_messages | Read output when COMPLETED |
| neo_send_feedback | Reply when WAITING_FOR_FEEDBACK |
| neo_pause_task | Pause a running task |
| neo_resume_task | Resume a paused task |
| neo_stop_task | Stop and clean up permanently |
`,
  },
  'neo://docs/workflow': {
    title: 'Typical Workflow',
    body: `# Neo MCP Workflow

neo_submit_task → neo_task_status (poll) → neo_get_messages

When WAITING_FOR_FEEDBACK: neo_send_feedback → neo_task_status again.
`,
  },
  'neo://docs/env': {
    title: 'Environment Variables',
    body: `# Environment Variables

NEO_SECRET_KEY (required): sk-v1-... from heyneo.com/dashboard

NEO_WORKSPACE_DIR (required for Postman): absolute project/git root.

Path args on prompts are workspace-relative, e.g. data/fraud.csv
`,
  },
  'neo://docs/prompts': {
    title: 'Example Prompts',
    body: `# Example Prompts

10 prompts: train-model, fine-tune-classifier, fine-tune-llm, build-rag-pipeline, build-ai-agent, fix-training-run, build-ml-pipeline, benchmark-prompts, run-eda, train-vision-model.

Path args are optional — omit to use workspace defaults.
`,
  },
};

function promptMessage(text: string) {
  return {
    messages: [{ role: 'user' as const, content: { type: 'text' as const, text } }],
  };
}

const pathSchema = z.string().optional().describe('Workspace-relative path, e.g. data/fraud.csv');
const goalSchema = z.string().optional().describe('Optional goal or metric');

export function registerPostmanCapabilities(server: McpServerLike): void {
  server.registerPrompt(
    'train-model',
    {
      title: 'Train ML Model',
      description: 'Train an ML model on tabular data',
      argsSchema: { path: pathSchema, goal: goalSchema },
    },
    async (args: { path?: string; goal?: string }) =>
      promptMessage(
        `Use Neo to train a machine learning model on ${optPath(args, 'the main dataset in the workspace')}. ` +
          `${args.goal ?? 'Evaluate metrics, save the model and a short report to the workspace.'}`,
      ),
  );

  server.registerPrompt(
    'fine-tune-classifier',
    {
      title: 'Fine-tune Classifier',
      description: 'Fine-tune a text classifier with cross-validation',
      argsSchema: { path: pathSchema },
    },
    async (args: { path?: string }) =>
      promptMessage(
        `Use Neo to fine-tune a text classifier on ${optPath(args, 'labeled text data in the workspace')} with 5-fold cross-validation.`,
      ),
  );

  server.registerPrompt(
    'fine-tune-llm',
    {
      title: 'Fine-tune LLM',
      description: 'Fine-tune an open LLM on local data',
      argsSchema: {
        base_model: z.string().optional().describe('Exact model ID, e.g. meta-llama/Llama-3.1-8B'),
        path: pathSchema,
        goal: goalSchema,
      },
    },
    async (args: { base_model?: string; path?: string; goal?: string }) =>
      promptMessage(
        `Use Neo to fine-tune ${args.base_model ?? 'an open LLM you select from the workspace context'} ` +
          `on ${optPath(args, 'instruction or completion data in the workspace')}. ` +
          `${args.goal ?? 'Use LoRA or QLoRA where appropriate and save adapters to the workspace.'}`,
      ),
  );

  server.registerPrompt(
    'build-rag-pipeline',
    {
      title: 'Build RAG Pipeline',
      description: 'Build a RAG pipeline over documents',
      argsSchema: { path: pathSchema, goal: goalSchema },
    },
    async (args: { path?: string; goal?: string }) =>
      promptMessage(
        `Use Neo to build a RAG pipeline over documents at ${optPath(args, './docs')}. ` +
          `${args.goal ?? 'Include ingestion, chunking, embeddings, vector store, and a query API with citations.'}`,
      ),
  );

  server.registerPrompt(
    'build-ai-agent',
    {
      title: 'Build AI Agent',
      description: 'Build an AI agent with tools and eval',
      argsSchema: {
        description: z.string().describe('Agent goal, tools, data sources, and constraints'),
      },
    },
    async ({ description }: { description: string }) =>
      promptMessage(
        `Use Neo to build an AI agent: ${description}. Include tool definitions, a runnable entrypoint, and basic eval or smoke tests.`,
      ),
  );

  server.registerPrompt(
    'fix-training-run',
    {
      title: 'Fix Training Run',
      description: 'Debug and re-run a failing training job',
      argsSchema: {
        context: z.string().describe('Error logs or what failed'),
      },
    },
    async ({ context }: { context: string }) =>
      promptMessage(`Use Neo to fix the failing training run and re-run with full logging. Context: ${context}`),
  );

  server.registerPrompt(
    'build-ml-pipeline',
    {
      title: 'Build ML Pipeline',
      description: 'Build or debug an end-to-end ML pipeline',
      argsSchema: {
        description: z.string().describe('Pipeline goal, data sources, and constraints'),
      },
    },
    async ({ description }: { description: string }) =>
      promptMessage(`Use Neo to build or debug an end-to-end ML pipeline: ${description}`),
  );

  server.registerPrompt(
    'benchmark-prompts',
    {
      title: 'Benchmark Prompts',
      description: 'Benchmark LLM prompts on an eval set',
      argsSchema: { path: pathSchema },
    },
    async (args: { path?: string }) =>
      promptMessage(
        `Use Neo to benchmark these prompts on our eval set at ${optPath(args, 'the evaluation dataset in the workspace')}. Report metrics and save results.`,
      ),
  );

  server.registerPrompt(
    'run-eda',
    {
      title: 'Run EDA',
      description: 'Exploratory data analysis with report',
      argsSchema: { path: pathSchema, goal: goalSchema },
    },
    async (args: { path?: string; goal?: string }) =>
      promptMessage(
        `Use Neo to run exploratory data analysis on ${optPath(args, 'the main dataset in the workspace')}. ` +
          `${args.goal ?? 'Produce summary stats, key plots, and a markdown report.'}`,
      ),
  );

  server.registerPrompt(
    'train-vision-model',
    {
      title: 'Train Vision Model',
      description: 'Train a computer vision model',
      argsSchema: { path: pathSchema, goal: goalSchema },
    },
    async (args: { path?: string; goal?: string }) =>
      promptMessage(
        `Use Neo to train a computer vision model on ${optPath(args, 'image data in the workspace')}. ` +
          `${args.goal ?? 'Train, evaluate, and save weights plus a brief eval report.'}`,
      ),
  );

  for (const [uri, { title, body }] of Object.entries(RESOURCE_DOCS)) {
    const slug = title.toLowerCase().replace(/\s+/g, '-');
    server.registerResource(
      slug,
      uri,
      { title, description: title, mimeType: 'text/markdown' },
      async () => ({
        contents: [{ uri, mimeType: 'text/markdown', text: body }],
      }),
    );
  }
}
