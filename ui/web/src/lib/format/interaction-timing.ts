import { track } from "../telemetry/telemetry";

const MAX_COMPOSER_LATENCY_MS = 120_000;
const pendingMessageSentAt = new Map<number, number>();
const testHooks: { onNoteTurnStart?: (agentId: number) => void } = {};

export function markMessageSent(agentId: number): void {
  pendingMessageSentAt.set(agentId, performance.now());
}

export function clearMessageSent(agentId: number): void {
  pendingMessageSentAt.delete(agentId);
}

export function noteTurnStart(agentId: number): void {
  testHooks.onNoteTurnStart?.(agentId);
  const sentAt = pendingMessageSentAt.get(agentId);
  if (sentAt === undefined) return;
  pendingMessageSentAt.delete(agentId);
  const latencyMs = performance.now() - sentAt;
  if (latencyMs <= 0 || latencyMs > MAX_COMPOSER_LATENCY_MS) return;
  track("composer-latency", {
    key: "send-to-turn-start",
    value: Math.round(latencyMs),
  });
}

export function interactionTimingResetForTest(
  onNoteTurnStart?: (agentId: number) => void,
): void {
  pendingMessageSentAt.clear();
  if (onNoteTurnStart) testHooks.onNoteTurnStart = onNoteTurnStart;
  else delete testHooks.onNoteTurnStart;
}
