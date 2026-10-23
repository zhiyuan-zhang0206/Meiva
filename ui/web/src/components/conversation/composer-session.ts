export const draftKey = (id: number) => `composer-draft-${id}`;
export const sendAttemptKey = (id: number) => `composer-send-attempt-${id}`;

export interface SendAttempt {
  signature: string;
  clientMessageId: string;
  agentId: number | null;
  content: string;
  imageUrls: string[];
}

export function isAttempt(attempt: SendAttempt | null, clientMessageId: string): boolean {
  return attempt?.clientMessageId === clientMessageId;
}

const memorySession = new Map<string, string | null>();

export function readSession(key: string): string | null {
  if (memorySession.has(key)) return memorySession.get(key) ?? null;
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeSession(key: string, value: string): void {
  memorySession.set(key, value);
  try {
    sessionStorage.setItem(key, value);
  } catch {
    return;
  }
  memorySession.delete(key);
}

export function removeSession(key: string): void {
  memorySession.set(key, null);
  try {
    sessionStorage.removeItem(key);
  } catch {
    return;
  }
  memorySession.delete(key);
}
