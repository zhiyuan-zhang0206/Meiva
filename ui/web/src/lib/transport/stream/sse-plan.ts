export type SseChannel = "system" | "alerts" | "systemAll";
export type SseSlot = SseChannel | "merged";
export type SseState = "open" | "reconnecting" | "closed";
export type ReadyStateKind = "connecting" | "open" | "closed";

export interface SseListener {
  onFrame: (raw: string) => void;
  onState: (state: SseState) => void;
  onGap?: (dropped: number) => void;
}

export const SSE_INIT: EventSourceInit = Object.freeze({ withCredentials: true });

export function reconnectDelay(failures: number, baseMs: number, maxMs: number): number {
  return Math.min(baseMs * 2 ** failures, maxMs);
}

export function readyStateKind(readyState: number): ReadyStateKind {
  switch (readyState) {
    case 0:
      return "connecting";
    case 1:
      return "open";
    case 2:
      return "closed";
    default:
      throw new Error(`unknown EventSource readyState: ${readyState}`);
  }
}

export function probeVerdict(probe: { authenticated: boolean }): "invalid-session" | "reopen" {
  return probe.authenticated ? "reopen" : "invalid-session";
}
