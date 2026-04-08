#!/usr/bin/env node
/**
 * neo-mcp-daemon — entry point
 *
 * Usage:
 *   npx neo-mcp-daemon [/path/to/workspace] [--deployment-id UUID]
 *
 * Environment:
 *   NEO_SECRET_KEY      — API key (sk-v1-...)  — primary auth
 *   NEO_DEPLOYMENT_ID   — optional UUID override
 *   NEO_API_URL         — optional, defaults to https://master.heyneo.so
 */

import { resolve } from 'path';
import { runDaemon } from './daemon.js';
import { runMcpServer } from './mcp-server.js';

function parseArgs(): { workspace?: string; deploymentId?: string; mcp: boolean } {
  const args = process.argv.slice(2);
  let workspace: string | undefined;
  let deploymentId: string | undefined;
  let mcp = false;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--deployment-id' && args[i + 1]) {
      deploymentId = args[++i];
    } else if (args[i] === '--mcp') {
      mcp = true;
    } else if (args[i] && !args[i].startsWith('-')) {
      workspace = args[i];
    }
  }
  return { workspace, deploymentId, mcp };
}

const { workspace, deploymentId, mcp } = parseArgs();

// NEO_WORKSPACE_DIR env var mirrors Python: os.environ.get("NEO_WORKSPACE_DIR", os.getcwd())
const envWorkspaceDir = process.env['NEO_WORKSPACE_DIR'];

if (mcp) {
  // MCP server mode: expose Neo tools to Claude Code and run daemon in background.
  // Usage: claude mcp add --scope user neo -e NEO_SECRET_KEY=sk-v1-... -- npx neo-mcp-daemon --mcp
  const effectiveWorkspace = resolve(workspace ?? envWorkspaceDir ?? process.cwd());
  runMcpServer({ workspace: effectiveWorkspace, deploymentId }).catch((e: unknown) => {
    process.stderr.write(`Fatal MCP server error: ${e}\n`);
    process.exit(1);
  });
} else {
  // Daemon-only mode (default): poll Neo backend and execute tasks locally.
  const effectiveWorkspace = workspace ?? envWorkspaceDir;
  runDaemon({ workspace: effectiveWorkspace, deploymentId }).catch((e: unknown) => {
    console.error('Fatal daemon error:', e);
    process.exit(1);
  });
}
