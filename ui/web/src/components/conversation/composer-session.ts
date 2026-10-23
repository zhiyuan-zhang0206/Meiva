// meiva:note
// One session owner retains browser storage and module-lifetime fallback state for draft and submit identity.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { newOperationKey as newClientMessageId } from "@/lib/transport/operation-key";

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

export function readSendAttempt(agentId: number): SendAttempt | null {
  try {
    const parsed = JSON.parse(readSession(sendAttemptKey(agentId)) ?? "null") as unknown;
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

export function resolveSendAttempt(
  current: SendAttempt | null,
  sendingAgentId: number | null,
  content: string,
  imageUrls: string[],
): SendAttempt {
  const signature = JSON.stringify([sendingAgentId, content, imageUrls]);
  let candidate = current;
  if (candidate?.signature !== signature && sendingAgentId != null) {
    const persisted = readSendAttempt(sendingAgentId);
    candidate = persisted?.signature === signature ? persisted : null;
  }
  return candidate?.signature === signature
    ? candidate
    : {
        signature,
        clientMessageId: newClientMessageId(),
        agentId: sendingAgentId,
        content,
        imageUrls: [...imageUrls],
      };
}
