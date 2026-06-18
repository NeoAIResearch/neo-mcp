/**
 * MCP prompts and resources for Postman and other MCP clients.
 */

import { z } from 'zod';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type McpServerLike = any;

const RESOURCE_DOCS: Record<string, { title: string; body: string }> = {
  'neo://docs/overview': {
    title: 'Neo MCP Overview',
    body: `# Neo MCP Overview

Neo MCP connects Neo — an autonomous AI engineer for ML, LLM, and data workflows — to MCP clients like Postman.

- Submit AI/ML tasks in plain English
- Local daemon writes files to your machine (local-first)
- Poll status, read output, manage task lifecycle

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
| neo_task_status | Check RUNNING / COMPLETED / WAITING_FOR_FEEDBACK / PAUSED |
| neo_get_messages | Read output when COMPLETED |
| neo_send_feedback | Reply when WAITING_FOR_FEEDBACK |
| neo_pause_task | Pause a running task |
| neo_resume_task | Resume a paused task |
| neo_stop_task | Stop and clean up permanently |
| neo_list_integrations | List stored integration names |
| neo_add_integration | Register a credential |
| neo_test_integration | Verify a stored key |
| neo_remove_integration | Delete a stored key |
`,
  },
  'neo://docs/workflow': {
    title: 'Typical Workflow',
    body: `# Neo MCP Workflow

neo_submit_task → neo_task_status (poll) → neo_get_messages

When WAITING_FOR_FEEDBACK: neo_send_feedback → neo_task_status again.

Reconnect: neo_list_tasks → neo_task_status → neo_get_messages
`,
  },
  'neo://docs/env': {
    title: 'Environment Variables',
    body: `# Environment Variables

NEO_SECRET_KEY (required): sk-v1-... from heyneo.com/dashboard

NEO_WORKSPACE_DIR (required for Postman): absolute project/git root where Neo writes files.

NEO_ENVIRONMENT (optional): production (default) or staging
`,
  },
};

function promptMessage(text: string) {
  return {
    messages: [{ role: 'user' as const, content: { type: 'text' as const, text } }],
  };
}

export function registerPostmanCapabilities(server: McpServerLike): void {
  server.registerPrompt(
    'train-model',
    {
      title: 'Train ML Model',
      description: 'Train an ML model on a local dataset using Neo',
      argsSchema: {
        dataset_path: z.string().describe('Path to CSV or data file'),
        goal: z.string().optional().describe('e.g. optimize for recall'),
      },
    },
    async ({ dataset_path, goal }: { dataset_path: string; goal?: string }) =>
      promptMessage(
        `Use Neo to train a model on data at ${dataset_path}. ${goal ?? 'Evaluate metrics and save the model to the workspace.'}`,
      ),
  );

  server.registerPrompt(
    'fine-tune-classifier',
    {
      title: 'Fine-tune Classifier',
      description: 'Fine-tune a text classifier with cross-validation',
      argsSchema: {
        data_path: z.string().describe('Path to training data'),
      },
    },
    async ({ data_path }: { data_path: string }) =>
      promptMessage(
        `Use Neo to fine-tune a text classifier on ${data_path} with 5-fold cross-validation.`,
      ),
  );

  server.registerPrompt(
    'fix-training-run',
    {
      title: 'Fix Training Run',
      description: 'Debug and re-run a failing training job with logging',
      argsSchema: {
        context: z.string().describe('What failed or relevant error output'),
      },
    },
    async ({ context }: { context: string }) =>
      promptMessage(
        `Use Neo to fix the failing training run and re-run with logging. Context: ${context}`,
      ),
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
      description: 'Benchmark prompts on an evaluation set',
      argsSchema: {
        eval_set_path: z.string().describe('Path to eval dataset or prompt set'),
      },
    },
    async ({ eval_set_path }: { eval_set_path: string }) =>
      promptMessage(`Use Neo to benchmark these prompts on our eval set at ${eval_set_path}`),
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
