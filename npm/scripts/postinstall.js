#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');

// Auto-install the Claude Code skill so Claude knows to route AI/ML tasks to Neo.
try {
  const skillsDir = path.join(os.homedir(), '.claude', 'skills');
  const skillDest = path.join(skillsDir, 'neo.md');
  const skillSrc = path.join(__dirname, '..', 'skills', 'neo.md');

  if (fs.existsSync(skillSrc)) {
    fs.mkdirSync(skillsDir, { recursive: true });
    fs.copyFileSync(skillSrc, skillDest);
  }
} catch (_) {
  // Non-fatal — skill install is best-effort
}
