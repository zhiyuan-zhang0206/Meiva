import { probeVerdict, readyStateKind, reconnectDelay } from "./stream/sse-plan";
import type { EventSourceHandle, SessionPort } from "./transport";

export type SseAuthStatus = "loading" | "authenticated" | "unauthenticated";

export interface SseStoreSlice {
  reconnectNonce: number;
  bumpReconnect: () => void;
  activeId: number | null;
}

export interface SseRuntimeHooks {
  useAuthStatus: () => SseAuthStatus;
  useStore: <T>(selector: (state: SseStoreSlice) => T) => T;
  useMergedChannels: () => readonly ("system" | "alerts" | "systemAll")[];
  useDocumentVisible: () => boolean;
}

const DEFAULT_MERGED_CHANNELS: readonly ("system" | "alerts" | "systemAll")[] = [];
const DEFAULT_STORE_SLICE: SseStoreSlice = {
  reconnectNonce: 0,
  bumpReconnect: () => undefined,
  activeId: null,
};
const sessionInvalidSink: { handler: (() => void) | null } = { handler: null };

export function setSessionInvalidHandler(handler: (() => void) | null): void {
  sessionInvalidSink.handler = handler;
}

export function notifySessionInvalid(): void {
  sessionInvalidSink.handler?.();
}

export const sseRuntime: SseRuntimeHooks = {
  useAuthStatus: () => "authenticated",
  useStore: (selector) => selector(DEFAULT_STORE_SLICE),
  useMergedChannels: () => DEFAULT_MERGED_CHANNELS,
  useDocumentVisible: () => true,
};

export function registerSseRuntime(hooks: Partial<SseRuntimeHooks>): void {
  Object.assign(sseRuntime, hooks);
}

export interface SseTimerPolicy {
  failCount: { current: number };
  watchdogMs: number;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
}

export interface SseTimers {
  armWatchdog(onFire: () => void): void;
  clearWatchdog(): void;
  scheduleReopen(onReopen: () => void): void;
  resetBackoff(): void;
  dispose(): void;
  isDisposed(): boolean;
}

// meiva:note
// The two timers one SSE connection owns. The watchdog is re-armed by any frame
// and fires after a silent window; a reopen is single-flight (a second request
// while one is pending is dropped) and waits a capped exponential delay that grows
// with each consecutive failure and resets on a successful open. Disposal cancels
// both and makes every later request a no-op.
// meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
export function createSseTimers(policy: SseTimerPolicy): SseTimers {
  let disposed = false;
  let watchdog: ReturnType<typeof setTimeout> | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const clearWatchdog = () => {
    if (watchdog !== null) clearTimeout(watchdog);
    watchdog = null;
  };

  return {
    armWatchdog: (onFire) => {
      clearWatchdog();
      watchdog = setTimeout(onFire, policy.watchdogMs);
    },
    clearWatchdog,
    scheduleReopen: (onReopen) => {
      if (retryTimer !== null) return;
      const delay = reconnectDelay(policy.failCount.current, policy.reconnectBaseMs, policy.reconnectMaxMs);
      policy.failCount.current += 1;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        onReopen();
      }, delay);
    },
    resetBackoff: () => { policy.failCount.current = 0; },
    dispose: () => {
      disposed = true;
      clearWatchdog();
      if (retryTimer !== null) clearTimeout(retryTimer);
    },
    isDisposed: () => disposed,
  };
}

// meiva:note
// After a legacy source the server closed, ask the session once: an invalid
// session stops here (the app returns to login), anything else - including an
// unreachable gateway, which is transient - schedules the capped-backoff reopen.
// meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
function probeThenReopen(session: SessionPort, timers: SseTimers, reopen: () => void): void {
  session.checkAuth()
    .then((result) => {
      if (timers.isDisposed()) return;
      if (probeVerdict(result) === "invalid-session") {
        session.sessionInvalid();
        return;
      }
      timers.scheduleReopen(reopen);
    })
    .catch(() => {
      if (timers.isDisposed()) return;
      timers.scheduleReopen(reopen);
    });
}

export function reopenIfClosed(
  source: EventSourceHandle,
  session: SessionPort,
  timers: SseTimers,
  reopen: () => void,
): void {
  if (readyStateKind(source.readyState) === "closed") probeThenReopen(session, timers, reopen);
}
