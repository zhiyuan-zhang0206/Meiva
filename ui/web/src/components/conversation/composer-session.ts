// meiva:note
// One session owner retains the composer's drafts and submit identity: the page's
// session storage when it has it, and this page view's memory when it does not, so
// a refused storage costs the draft at the next visit rather than the draft now.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import type { BrowserStorage } from "@/lib/browser/browser";

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

export function readSession(session: BrowserStorage, key: string): string | null {
  if (memorySession.has(key)) return memorySession.get(key) ?? null;
  const stored = session.read(key);
  switch (stored.state) {
    case "found":
      return stored.value;
    case "absent":
      return null;
    case "unavailable":
      return null;
  }
}

export function writeSession(session: BrowserStorage, key: string, value: string): void {
  const written = session.write(key, value);
  if (written === "stored") {
    memorySession.delete(key);
    return;
  }
  memorySession.set(key, value);
}

export function removeSession(session: BrowserStorage, key: string): void {
  const removed = session.remove(key);
  if (removed === "removed") {
    memorySession.delete(key);
    return;
  }
  memorySession.set(key, null);
}

export function readSendAttempt(session: BrowserStorage, agentId: number): SendAttempt | null {
  try {
    const parsed = JSON.parse(readSession(session, sendAttemptKey(agentId)) ?? "null") as unknown;
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !("signature" in parsed) ||
      typeof parsed.signature !== "string" ||
      !("clientMessageId" in parsed) ||
      typeof parsed.clientMessageId !== "string" ||
      !("content" in parsed) ||
      typeof parsed.content !== "string" ||
      !("imageUrls" in parsed) ||
      !Array.isArray(parsed.imageUrls) ||
      !parsed.imageUrls.every((url) => typeof url === "string")
    ) {
      return null;
    }
    return parsed as SendAttempt;
  } catch {
    return null;
  }
}

// meiva:note
// `mintClientMessageId` is the transport's own key source: a message id has to survive a retry of
// the same attempt, and which origin can mint one (crypto.randomUUID, or random bytes on plain
// HTTP) is the transport's business, not a component's.
export function resolveSendAttempt(
  session: BrowserStorage,
  current: SendAttempt | null,
  sendingAgentId: number | null,
  content: string,
  imageUrls: string[],
  mintClientMessageId: () => string,
): SendAttempt {
  const signature = JSON.stringify([sendingAgentId, content, imageUrls]);
  let candidate = current;
  if (candidate?.signature !== signature && sendingAgentId != null) {
    const persisted = readSendAttempt(session, sendingAgentId);
    candidate = persisted?.signature === signature ? persisted : null;
  }
  return candidate?.signature === signature
    ? candidate
    : {
        signature,
        clientMessageId: mintClientMessageId(),
        agentId: sendingAgentId,
        content,
        imageUrls: [...imageUrls],
      };
}
