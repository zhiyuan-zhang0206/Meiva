import { track } from "../telemetry/telemetry";

const MAX_COMPOSER_LATENCY_MS = 120_000;

interface InteractionTimingState {
  pendingMessageSentAt: Map<number, number>;
  nowOverride: (() => number) | null;
  onNoteTurnStart?: (agentId: number) => void;
}

const state: InteractionTimingState = {
  pendingMessageSentAt: new Map<number, number>(),
  nowOverride: null,
};

export function markMessageSent(
  agentId: number,
  nowMs: number = state.nowOverride ? state.nowOverride() : performance.now(),
): void {
  state.pendingMessageSentAt.set(agentId, nowMs);
}

export function clearMessageSent(agentId: number): void {
  state.pendingMessageSentAt.delete(agentId);
}

export function noteTurnStart(
  agentId: number,
  nowMs: number = state.nowOverride ? state.nowOverride() : performance.now(),
): void {
  state.onNoteTurnStart?.(agentId);
  const sentAt = state.pendingMessageSentAt.get(agentId);
  if (sentAt === undefined) return;
  state.pendingMessageSentAt.delete(agentId);
  const latencyMs = nowMs - sentAt;
  if (latencyMs <= 0 || latencyMs > MAX_COMPOSER_LATENCY_MS) return;
  track("composer-latency", {
    key: "send-to-turn-start",
    value: Math.round(latencyMs),
  });
}

export function interactionTimingResetForTest(
  onNoteTurnStart?: (agentId: number) => void,
  nowOverride?: (() => number) | null,
): void {
  state.pendingMessageSentAt.clear();
  state.nowOverride = nowOverride ?? null;
  if (onNoteTurnStart) state.onNoteTurnStart = onNoteTurnStart;
  else delete state.onNoteTurnStart;
}
