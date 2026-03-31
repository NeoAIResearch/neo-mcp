#!/usr/bin/env node
/**
 * postinstall.js — installs the platform-specific Go daemon binary to ~/.neo/agent
 *
 * Called automatically by npm after `npm install` or `npx neo-mcp-daemon`.
 * Safe to re-run: idempotent, never overwrites a newer binary.
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const BINARY_MAP = {
  'darwin-arm64':  'neo-agent-mac',
  'darwin-x64':    'neo-agent-mac-intel',
  'linux-x64':     'neo-agent-linux',
  'linux-arm64':   'neo-agent-linux-arm',
  'win32-x64':     'neo-agent.exe',
};

function installGoBinary() {
  const platform = process.platform;
  const arch = process.arch;
  const key = `${platform}-${arch}`;

  const binaryName = BINARY_MAP[key];
  if (!binaryName) {
    // Not a fatal error — Node.js daemon will be used as fallback.
    console.log(`[neo-mcp-daemon] No Go binary for ${key} — Node.js daemon will be used.`);
    return;
  }

  const srcPath = path.join(__dirname, '..', 'binaries', binaryName);
  if (!fs.existsSync(srcPath)) {
    console.log(`[neo-mcp-daemon] Binary not found at ${srcPath} — skipping Go install.`);
    return;
  }

  const neoDir = path.join(os.homedir(), '.neo');
  const destPath = path.join(neoDir, 'agent');
  if (platform === 'win32') {
    destPath += '.exe';  // not reassignable with const, handled below
  }
  const destFinal = platform === 'win32'
    ? path.join(neoDir, 'agent.exe')
    : path.join(neoDir, 'agent');

  try {
    fs.mkdirSync(neoDir, { recursive: true });
    fs.copyFileSync(srcPath, destFinal);
    if (platform !== 'win32') {
      fs.chmodSync(destFinal, 0o755);
    }
    console.log(`[neo-mcp-daemon] Installed Go daemon → ${destFinal}`);
  } catch (err) {
    // Don't fail the install — just warn.
    console.warn(`[neo-mcp-daemon] Could not install Go binary: ${err.message}`);
  }
}

installGoBinary();
