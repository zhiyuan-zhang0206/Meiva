// Frontend user-modeling telemetry — the single tracking entry point.
//
// Pipeline: track() → in-memory buffer → batched POST to the gateway's
// /api/frontend-telemetry (sendBeacon when the tab is hiding, fetch +
// keepalive otherwise) → `frontend_interaction` events in the stream →
// the Grafana core panels (interactions / top elements / page views /
// settings changes).
//
// Volume discipline (task #1092 — the event stream must not be flooded):
//   1. dedupe — the same (page, element, key) at most once per 2 s;
//   2. rate cap — at most 100 events per minute per tab, excess dropped;
//   3. buffer cap — 200 pending events, oldest dropped.
// The gateway backstops with its own per-session limit, so a bug here can
// only waste this tab's budget, never the stream.
//
// No free text is ever collected: `element` is a closed union of known
// interaction points, `page` is the normalized router pathname, and
// instrumentation keys + values are bounded identifiers and sanitized
// ≤64-char scalars. Session id is a random per-tab uuid — it groups one
// browser session without carrying identity.

import type { Browser } from "@/lib/browser/browser";
import { API_BASE, registerTransportTelemetry } from "../transport/api-base";
import { newOperationKey } from "../transport/operation-key";
import { systemWallNow } from "../transport/system-transport";
import {
  createTelemetryBufferState,
  recordTrackEvent,
  resetTelemetryBufferState,
  takeFlushBatch,
  type TelemetryElement,
  type TrackOptions,
} from "./telemetry-plan";

export { normalizePage, type TelemetryElement, type TrackOptions } from "./telemetry-plan";

// Flush cadence while the tab is visible.
const FLUSH_INTERVAL_MS = 15_000;

// ── module state (browser only; everything is a no-op under SSR) ──

const runtimeState: {
  bufferState: ReturnType<typeof createTelemetryBufferState>;
  flushTimer: ReturnType<typeof setInterval> | null;
  warnedOnce: boolean;
  fetchImpl: ((url: string, init: RequestInit) => Promise<unknown>) | undefined;
  wallNow: () => number;
  browser: Browser | null;
} = {
  bufferState: createTelemetryBufferState(typeof window !== "undefined"),
  flushTimer: null,
  warnedOnce: false,
  fetchImpl: undefined,
  wallNow: systemWallNow,
  browser: null,
};

// meiva:note
// The composition root wires the page in: `Providers` hands the browser port this
// module sends through. Until it does — a module graph no root mounted, which is
// a unit test of some other component — a flush has no page to deliver from and
// sends nothing, so a test never reaches the network by accident. The telemetry
// tests wire a fake through this same function, not through a backdoor.
export function initTelemetry(browser: Browser): void {
  runtimeState.browser = browser;
}

function flush(): void {
  const { bufferState, browser } = runtimeState;
  if (browser === null || !bufferState.enabled || bufferState.buffer.length === 0) return;
  if (bufferState.sessionId === "") bufferState.sessionId = newOperationKey();
  const batch = takeFlushBatch(bufferState, bufferState.sessionId, runtimeState.wallNow(), API_BASE);
  if (batch === null) return;
  // Beacon first: it survives pagehide/unload and needs no response. When
  // unavailable (or its size limit rejects the batch), fall back to fetch
  // with keepalive.
  const ok = browser.beacon.send(batch.url, batch.body);
  if (!ok) {
    try {
      const sendFetch = runtimeState.fetchImpl ?? fetch;
      void sendFetch(batch.url, {
        method: "POST",
        credentials: "include",
        keepalive: true,
        headers: { "content-type": "application/json" },
        body: batch.body,
      }).catch(() => {
        if (!runtimeState.warnedOnce) {
          runtimeState.warnedOnce = true;
          console.warn("[telemetry] flush failed — interactions dropped");
        }
      });
    } catch {
      // keepalive unsupported / body too large — drop silently
    }
  }
}

const testListeners: {
  onTrack?: (element: TelemetryElement, opts?: TrackOptions) => void;
  onSetPage?: (page: string) => void;
  onFlush?: () => void;
} = {};

/** Flush buffered telemetry immediately. Lifecycle instrumentation calls this
 * after enqueueing final page metrics so delivery never depends on listener
 * registration order. */
export function flushTelemetry(): void {
  if (testListeners.onFlush) {
    testListeners.onFlush();
    return;
  }
  flush();
}

function ensureTimer(): void {
  if (runtimeState.flushTimer !== null) return;
  runtimeState.flushTimer = setInterval(flush, FLUSH_INTERVAL_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
  window.addEventListener("pagehide", flush);
  window.addEventListener("beforeunload", flush);
}

/** Track one interaction. No-op under SSR and while disabled; rate-limits in
 *  place and dedupes unless `dedupe: false`. Never throws. */
export function track(element: TelemetryElement, opts: TrackOptions = {}): void {
  if (testListeners.onTrack) {
    testListeners.onTrack(element, opts);
    return;
  }
  if (!recordTrackEvent(runtimeState.bufferState, element, opts, runtimeState.wallNow())) {
    return;
  }
  ensureTimer();
}

/** Set the current page (normalized route) — called by TelemetryPageView
 *  on every route change; subsequent track() calls inherit it. */
export function setTelemetryPage(page: string): void {
  if (testListeners.onSetPage) {
    testListeners.onSetPage(page);
  }
  runtimeState.bufferState.currentPage = page;
}

// ── test surface (not part of the public contract) ──

/** Reset module state — tests only. */
function __telemetryResetForTest(): void {
  resetTelemetryBufferState(runtimeState.bufferState, typeof window !== "undefined");
  if (runtimeState.flushTimer !== null) {
    clearInterval(runtimeState.flushTimer);
    runtimeState.flushTimer = null;
  }
  runtimeState.warnedOnce = false;
  delete runtimeState.fetchImpl;
  runtimeState.wallNow = systemWallNow;
  runtimeState.browser = null;
  delete testListeners.onTrack;
  delete testListeners.onSetPage;
  delete testListeners.onFlush;
  registerTransportTelemetry(track);
}

registerTransportTelemetry(track);

/** Force a flush now — tests only. Returns the number of events sent. */
function __telemetryFlushForTest(): number {
  const n = runtimeState.bufferState.buffer.length;
  flush();
  return n;
}

/** Pending buffer size — tests only. */
function __telemetryBufferSize(): number {
  return runtimeState.bufferState.buffer.length;
}

/** Per-tab session id (minted lazily) — tests only. */
function __telemetrySessionId(): string {
  if (runtimeState.bufferState.sessionId === "") {
    runtimeState.bufferState.sessionId = newOperationKey();
  }
  return runtimeState.bufferState.sessionId;
}

/** Force-disable (SSR-safety probe) — tests only. */
function __telemetrySetEnabledForTest(value: boolean): void {
  runtimeState.bufferState.enabled = value;
}

export const telemetryTestControl = Object.freeze({
  reset: __telemetryResetForTest,
  flush: __telemetryFlushForTest,
  bufferSize: __telemetryBufferSize,
  sessionId: __telemetrySessionId,
  setEnabled: __telemetrySetEnabledForTest,
  setFetch(fetchImpl: (url: string, init: RequestInit) => Promise<unknown>): void {
    runtimeState.fetchImpl = fetchImpl;
  },
  setWallNow(wallNow: () => number): void {
    runtimeState.wallNow = wallNow;
  },
  intercept(handlers: {
    onTrack?: (element: TelemetryElement, opts?: TrackOptions) => void;
    onSetPage?: (page: string) => void;
    onFlush?: () => void;
  }): void {
    testListeners.onTrack = handlers.onTrack;
    testListeners.onSetPage = handlers.onSetPage;
    testListeners.onFlush = handlers.onFlush;
  },
});
