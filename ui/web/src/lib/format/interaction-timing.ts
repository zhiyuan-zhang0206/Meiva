import { track } from "../telemetry/telemetry";

const MAX_COMPOSER_LATENCY_MS = 120_000;

interface InteractionTimingState {
  pendingMessageSentAt: Map<number, number>;
  onNoteTurnStart?: (agentId: number) => void;
}

const state: InteractionTimingState = {
  pendingMessageSentAt: new Map<number, number>(),
};

export function markMessageSent(agentId: number, nowMs?: number): void {
  state.pendingMessageSentAt.set(agentId, nowMs ?? performance.now());
}

export function clearMessageSent(agentId: number): void {
  state.pendingMessageSentAt.delete(agentId);
}

export function noteTurnStart(agentId: number, nowMs?: number): void {
  state.onNoteTurnStart?.(agentId);
  const sentAt = state.pendingMessageSentAt.get(agentId);
  if (sentAt === undefined) return;
  state.pendingMessageSentAt.delete(agentId);
  const latencyMs = (nowMs ?? performance.now()) - sentAt;
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
