import { mkdtempSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

// Ensure daemon tests never write to the real home directory.
if (!process.env['NEO_HOME']) {
  process.env['NEO_HOME'] = mkdtempSync(join(tmpdir(), 'neo-home-'));
}
