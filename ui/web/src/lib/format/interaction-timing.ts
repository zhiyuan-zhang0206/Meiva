import { track } from "../telemetry/telemetry";

const MAX_COMPOSER_LATENCY_MS = 120_000;

interface InteractionTimingState {
  pendingMessageSentAt: Map<number, number>;
  onNoteTurnStart?: (agentId: number) => void;
}

const state: InteractionTimingState = {
  pendingMessageSentAt: new Map<number, number>(),
};

// meiva:note
// Both ends of the measurement hand in the instant they read from the page's one wall
// clock (`useWallNow`), so this module keeps no clock of its own: a latency is the
// difference of two readings of that clock, and a test states the two instants instead
// of replacing a global.
export function markMessageSent(agentId: number, sentAtMs: number): void {
  state.pendingMessageSentAt.set(agentId, sentAtMs);
}

export function clearMessageSent(agentId: number): void {
  state.pendingMessageSentAt.delete(agentId);
}

export function noteTurnStart(agentId: number, turnStartMs: number): void {
  state.onNoteTurnStart?.(agentId);
  const sentAt = state.pendingMessageSentAt.get(agentId);
  if (sentAt === undefined) return;
  state.pendingMessageSentAt.delete(agentId);
  const latencyMs = turnStartMs - sentAt;
  if (latencyMs <= 0 || latencyMs > MAX_COMPOSER_LATENCY_MS) return;
  track("composer-latency", {
    key: "send-to-turn-start",
    value: Math.round(latencyMs),
  });
}

export function interactionTimingResetForTest(
  onNoteTurnStart?: (agentId: number) => void,
): void {
  state.pendingMessageSentAt.clear();
  if (onNoteTurnStart) state.onNoteTurnStart = onNoteTurnStart;
  else delete state.onNoteTurnStart;
}
