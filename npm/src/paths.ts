import { homedir } from 'os';
import { join } from 'path';

export const NEO_HOME = process.env['NEO_HOME'] || join(homedir(), '.neo');
export const DAEMON_DIR = join(NEO_HOME, 'daemon');
export const STANDALONE_UUID_FILE = join(DAEMON_DIR, 'standalone_deployment_id');
export const DAEMON_LOG = join(DAEMON_DIR, 'daemon.log');
export const NPM_PID_FILE = join(DAEMON_DIR, 'npm_daemon.pid');
export const WORKSPACES_FILE = join(DAEMON_DIR, 'thread-workspaces.json');

/** Per-deployment PID file — matches Python daemon's naming for compatibility. */
export function pidFileForDeployment(deploymentId: string): string {
  return join(DAEMON_DIR, `daemon_${deploymentId.slice(0, 8)}.pid`);
}
