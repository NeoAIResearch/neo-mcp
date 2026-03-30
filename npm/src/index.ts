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

import { runDaemon } from './daemon.js';

function parseArgs(): { workspace?: string; deploymentId?: string } {
  const args = process.argv.slice(2);
  let workspace: string | undefined;
  let deploymentId: string | undefined;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--deployment-id' && args[i + 1]) {
      deploymentId = args[++i];
    } else if (args[i] && !args[i].startsWith('-')) {
      workspace = args[i];
    }
  }
  return { workspace, deploymentId };
}

const { workspace, deploymentId } = parseArgs();
runDaemon({ workspace, deploymentId }).catch(err => {
  console.error('Fatal daemon error:', err);
  process.exit(1);
});
