/**
 * HTTP client for the Neo backend API.
 * Used by the MCP server to implement tool calls.
 */

import { NEO_API_URL } from './config';

const TIMEOUT_MS = 30_000;

async function fetchWithTimeout(url: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
}

function handleError(status: number): string {
  switch (status) {
    case 401: return 'Authentication failed. Check that NEO_SECRET_KEY is correct (sk-v1-...).';
    case 403: return 'Permission denied. Your key may not have access to this resource.';
    case 404: return 'Not found. The thread_id may be invalid or the task has expired.';
    case 429: return 'Rate limit exceeded. Wait a moment before retrying.';
    case 500: return 'Neo backend error. Try again in a moment.';
    default:  return `Neo API returned status ${status}.`;
  }
}

export async function submitTask(
  token: string,
  deploymentId: string,
  message: string,
  workspace: string,
): Promise<Record<string, unknown>> {
  const res = await fetchWithTimeout(`${NEO_API_URL}/v2/thread/init-chat-direct`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ message, deployment_id: deploymentId, deployment_type: 'vscode', workspace }),
  });
  if (!res.ok) throw new Error(handleError(res.status));
  return res.json() as Promise<Record<string, unknown>>;
}

export async function getTaskStatus(token: string, threadId: string): Promise<Record<string, unknown>> {
  const res = await fetchWithTimeout(`${NEO_API_URL}/v2/thread/status/${threadId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(handleError(res.status));
  return res.json() as Promise<Record<string, unknown>>;
}

export async function getMessages(
  token: string,
  threadId: string,
  before?: string,
  limit = 50,
): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ thread_id: threadId, limit: String(limit) });
  if (before) params.set('before', before);
  const res = await fetchWithTimeout(`${NEO_API_URL}/v2/thread/thread-messages?${params}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(handleError(res.status));
  return res.json() as Promise<Record<string, unknown>>;
}

export async function sendFeedback(token: string, threadId: string, message: string): Promise<void> {
  const res = await fetchWithTimeout(`${NEO_API_URL}/v2/thread/feedback/${threadId}`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw new Error(handleError(res.status));
}

export async function controlThread(
  token: string,
  threadId: string,
  signal: 'PAUSE' | 'RESUME',
): Promise<void> {
  const res = await fetchWithTimeout(`${NEO_API_URL}/v2/thread/control/${threadId}`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ signal }),
  });
  if (!res.ok) throw new Error(handleError(res.status));
}

export async function stopThread(token: string, threadId: string): Promise<void> {
  const res = await fetchWithTimeout(`${NEO_API_URL}/v2/thread/cleanup-direct/${threadId}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(handleError(res.status));
}
