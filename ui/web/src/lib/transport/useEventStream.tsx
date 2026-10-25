"use client";

// Profile-shared basic, detail, and alert streams use one visible leader's
// EventSources. Providers retain their per-page fold and subscriber contracts.
// Hidden pages leave the transport and reconcile authoritative reads on open.
// Alerts have a separate domain provider. Frames may contain a single event or
// a batch; batch consumers fold one frame in one state update.

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { trackTransport } from "./api-base";
import { useFoldOwner } from "../fold/owner";
import { createSseTimers, reopenIfClosed, sseRuntime } from "./sse-lifecycle";
import { classifySystemFrame, nextParseFailures, selectedAgentEvents } from "./sse-frames";
import { readyStateKind, type SseChannel } from "./stream/sse-plan";
import { useStreamHub } from "./transport-context";
import type { EventSourceHandle } from "./transport";
import type { ConnectionEvent, SystemEvent } from "../contracts/types";

export type { ConnectionEvent } from "../contracts/types";

// Half-dead-connection watchdog window. The server emits a heartbeat data
// frame after ~15s of silence (so `onmessage` sees liveness even when no
// business events flow), so under a healthy connection a frame arrives at
// least every ~15s. 45s with NO frame at all (not even a heartbeat) means
// the socket is wedged: a graceful server restart or a proxy hop that
// stayed OPEN but stopped delivering. EventSource's own onerror can't see
// this (readyState stays OPEN), so we detect it ourselves and force a
// clean reopen via bumpReconnect().
const WATCHDOG_MS = 45_000;

// SSE-connect-failure retry. EventSource auto-retries a *transient* network
// drop on its own (readyState → CONNECTING, onerror fires repeatedly), but on a
// non-2xx *response* — a 503 while the cluster is paused for a rollout or a 500 —
// it gives up permanently (readyState → CLOSED, onerror fires once and never
// again). A 401/403 stops through the session probe below; transient failures get
// a capped exponential backoff, single-flight (never stack timers), and reset the
// backoff on a successful open.
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

interface Subscriber {
  system: (ev: SystemEvent) => void;
  conn: (ev: ConnectionEvent) => void;
  /** Frame-batch delivery (optional): when present, ONE SSE frame's events
   * arrive in a single call instead of one `system` call per event. The
   * high-rate all-events stream carries the active agent's deltas batched at
   * up to 10 frames/s; a batch subscriber folds a whole frame inside one
   * store update — one notification + one render per frame instead of one
   * per event (the storm that janked the home page while busy agents
   * streamed). Subscribers without it keep the per-event contract. When
   * present, `system` is not called for that subscriber. */
  systemBatch?: (events: SystemEvent[]) => void;
}

export interface EventStreamContextValue {
  subscribe: (
    onSystem: (ev: SystemEvent) => void,
    onConnection: (ev: ConnectionEvent) => void,
    onSystemBatch?: (events: SystemEvent[]) => void,
  ) => () => void;
}

// meiva:note
// Fans one shared or legacy SSE channel into the unchanged subscriber API.
// `onOpenChange` drives the global Provider's e2e-ready marker. A cluster-update
// reconnect bump re-runs the effect; the leader replaces its source and the open
// that follows reconciles missed events. Unauthenticated or hidden pages keep the
// connection closed, because EventSource cannot send an Authorization header and
// an unauthenticated GET would only feed a retry storm. A legacy source that the
// server closes is probed once: an invalid session stops, anything else retries
// with capped exponential backoff.
// meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
interface SseConnectionResult {
  subscribe: EventStreamContextValue["subscribe"];
  sseOpen: boolean;
}

function useSseConnection(
  path: string | null,
  channel: SseChannel,
  activeId: number | null,
  isVisible: boolean,
): SseConnectionResult {
  const subscribersRef = useRef<Set<Subscriber>>(new Set());
  const [sseOpen, setSseOpen] = useState(false);
  const hub = useStreamHub();
  const authStatus = sseRuntime.useAuthStatus();
  const reconnectNonce = sseRuntime.useStore((s) => s.reconnectNonce);
  const bumpReconnect = sseRuntime.useStore((s) => s.bumpReconnect);
  // Legacy-only CLOSED retry, separate from the global reconnect lever.
  const [retryNonce, setRetryNonce] = useState(0);
  const failCountRef = useRef(0);
  const lastReconnectNonce = useRef(reconnectNonce);
  const transport = hub.transportFor(sseRuntime.useMergedChannels());

  useEffect(() => {
    if (authStatus !== "authenticated" || !isVisible || (!transport && path === null)) {
      lastReconnectNonce.current = reconnectNonce;
      return;
    }
    const mode = hub.modeEventOnce();
    if (mode !== null) trackTransport("sse-transport", mode);
    // meiva:note
    // Consecutive parse failures within one connection: the backend validates
    // every frame, so repeated parse errors mean the transport is corrupting
    // frames and the third forces a backoff reopen.
    let parseFailures = 0;
    let es: EventSourceHandle | null = null;
    if (!transport) {
      if (path === null) throw new Error("legacy SSE path missing");
      es = hub.openLegacy(path);
    }

    const timers = createSseTimers({
      failCount: failCountRef,
      watchdogMs: WATCHDOG_MS,
      reconnectBaseMs: RECONNECT_BASE_MS,
      reconnectMaxMs: RECONNECT_MAX_MS,
    });
    const notifyAll = (event: ConnectionEvent) => {
      for (const sub of subscribersRef.current) sub.conn(event);
    };
    const reopenLegacy = () => setRetryNonce((n) => n + 1);
    const armWatchdog = () => timers.armWatchdog(() => {
      notifyAll({ type: "reconnecting" });
      if (transport) transport.restart(channel);
      else bumpReconnect();
    });

    const handleOpen = () => {
      if (timers.isDisposed()) return;
      timers.resetBackoff();
      setSseOpen(true);
      armWatchdog();
      notifyAll({ type: "open" });
    };
    const handleFrame = (raw: string) => {
      if (timers.isDisposed()) return;
      armWatchdog();
      const frame = classifySystemFrame(raw);
      if (frame.kind === "heartbeat") return;
      if (frame.kind === "malformed") {
        notifyAll({ type: "parse-failed", raw, error: frame.error });
        const failures = nextParseFailures(parseFailures);
        parseFailures = failures.failures;
        if (!failures.reopen) return;
        if (transport) {
          if (transport.isLeader()) transport.restart(channel, true);
          return;
        }
        if (es === null) throw new Error("legacy EventSource unavailable", { cause: frame.error });
        es.close();
        timers.scheduleReopen(reopenLegacy);
        return;
      }

      // The leader relays the union detail feed. Preserve each tab's old
      // selected-agent stream contract before any subscriber sees a frame.
      const events = transport && channel === "systemAll"
        ? selectedAgentEvents(frame.events, activeId)
        : frame.events;
      if (transport && channel === "systemAll" && events.length === 0) return;

      // Isolate subscriber failures; batch subscribers receive one call per frame.
      for (const sub of subscribersRef.current) {
        try {
          if (sub.systemBatch) {
            sub.systemBatch(events);
          } else {
            for (const event of events) {
              sub.system(event);
            }
          }
        } catch (subErr) {
          hub.transport.diagnostics.error("[useSseConnection] subscriber threw while folding an event", subErr);
        }
      }
    };
    if (transport) {
      const unsubscribe = transport.subscribe(channel, {
        onFrame: handleFrame,
        onGap: () => handleOpen(),
        onState: (state) => {
          if (timers.isDisposed()) return;
          if (state === "open") {
            handleOpen();
          } else {
            if (state === "closed") {
              timers.clearWatchdog();
            }
            notifyAll({ type: state });
          }
        },
      }, activeId);
      if (lastReconnectNonce.current !== reconnectNonce) transport.restart(channel);
      lastReconnectNonce.current = reconnectNonce;
      return () => {
        setSseOpen(false);
        timers.dispose();
        unsubscribe();
      };
    }

    if (es === null) throw new Error("legacy EventSource unavailable");
    const source = es;
    source.onopen = handleOpen;
    source.onmessage = (event) => handleFrame(
      typeof event.data === "string" ? event.data : "[non-string SSE payload]",
    );
    source.onerror = () => {
      if (timers.isDisposed()) return;
      const kind = readyStateKind(source.readyState);
      if (kind === "closed") notifyAll({ type: "closed" });
      else if (kind === "connecting") notifyAll({ type: "reconnecting" });
      reopenIfClosed(source, hub.session, timers, reopenLegacy);
    };

    return () => {
      setSseOpen(false);
      timers.dispose();
      source.close();
    };
    // meiva:note
    // path + authStatus + reconnectNonce + retryNonce are the levers (retryNonce is
    // the local connect-failure backoff). subscribersRef / bumpReconnect are
    // stable identities included only to satisfy the lint.
  }, [path, channel, activeId, isVisible, transport, authStatus, reconnectNonce, retryNonce,
    bumpReconnect, subscribersRef, hub]);

  const subscribe = useSubscribe(subscribersRef);
  const canConnect = authStatus === "authenticated" && isVisible && (Boolean(transport) || path !== null);
  return { subscribe, sseOpen: canConnect && sseOpen };
}

/** Mint a stable `subscribe(onSystem, onConn)` over a subscriber Set. */
function useSubscribe(
  subscribersRef: React.RefObject<Set<Subscriber>>,
): EventStreamContextValue["subscribe"] {
  return useCallback(
    (onSystem, onConnection, onSystemBatch) => {
      const handler: Subscriber = {
        system: onSystem,
        conn: onConnection,
        systemBatch: onSystemBatch,
      };
      subscribersRef.current.add(handler);
      return () => {
        subscribersRef.current.delete(handler);
      };
    },
    [subscribersRef],
  );
}

function useSubscribeEffect(
  ctx: EventStreamContextValue | null,
  hookName: string,
  providerName: string,
  onSystemEvent: (event: SystemEvent) => void,
  onConnectionEvent: (ev: ConnectionEvent) => void,
  onSystemBatch?: (events: SystemEvent[]) => void,
): void {
  if (!ctx) {
    throw new Error(`${hookName} must be used inside <${providerName}>`);
  }
  useEffect(() => {
    return ctx.subscribe(onSystemEvent, onConnectionEvent, onSystemBatch);
  }, [ctx, onSystemEvent, onConnectionEvent, onSystemBatch]);
}

// ---------------------------------------------------------------------------
// Global broadcast — cross-agent, low-frequency GLOBAL_ROLES for all agents.
// ---------------------------------------------------------------------------

export const EventStreamContext = createContext<EventStreamContextValue | null>(null);

/**
 * Provider for the global `/api/system` broadcast. The profile leader's
 * EventSource serves all visible pages; this page leaves while hidden.
 * Renders an `sse-ready` marker once OPEN — e2e tests
 * (`page.wait_for_selector('[data-testid="sse-ready"]')`) gate on it before
 * interacting, so SSE-driven UI isn't raced.
 */
export function EventStreamProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  // Reopening reconciles any events missed while the page was hidden.
  const isVisible = sseRuntime.useDocumentVisible();
  const { subscribe, sseOpen } = useSseConnection(
    isVisible ? "/api/system" : null,
    "system",
    null,
    isVisible,
  );

  // ── The fold (R4 layer 1): ONE subscriber owns every domain's snapshot×SSE
  // reconciliation — applyEvent folds system events into the query cache, and
  // the connection "open" handler runs the central reconnect reconcile
  // (invalidate all queries — events missed during a disconnect gap are
  // repaired wholesale). This replaced the per-hook folding skeletons
  // (useAgentsCacheSync and friends); hooks only read their keys now.
  const fold = useFoldOwner();

  // Subscribe the fold — it outlives every per-page subscriber (the connection
  // persists across navigation), so no lifecycle event is dropped while no page
  // reader is mounted. Standard effect: on dependency change React runs the
  // cleanup (unsubscribe) BEFORE the new setup (subscribe), so a re-render can
  // never leave the fold unsubscribed. The ref-guarded "subscribe once" variant
  // did exactly that — after the first cleanup it early-returned and the fold
  // stayed unsubscribed forever (Task #1033). useFoldOwner returns a stable
  // object, so in practice this effect runs once.
  useEffect(() => {
    return subscribe(fold.onSystemEvent, fold.onConnectionEvent);
  }, [subscribe, fold]);

  // value reference is stable (subscribe is from useCallback) — consumers
  // using useEffect deps don't trigger unnecessary unsubscribe/resubscribe
  // on Provider re-render.
  return (
    <EventStreamContext.Provider value={{ subscribe }}>
      {children}
      {sseOpen ? <div data-testid="sse-ready" className="hidden" aria-hidden /> : null}
    </EventStreamContext.Provider>
  );
}


/**
 * Subscribe to the global broadcast. Must be called inside
 * `<EventStreamProvider>`.
 *
 * `onConnectionEvent` is required — do not let silent failure sneak
 * back through a default fallback. Callers that don't care about
 * connection state must still pass an explicit noop with a comment
 * explaining why.
 */
export function useEventStream(
  onSystemEvent: (event: SystemEvent) => void,
  onConnectionEvent: (ev: ConnectionEvent) => void,
): void {
  const ctx = useContext(EventStreamContext);
  useSubscribeEffect(ctx, "useEventStream", "EventStreamProvider", onSystemEvent, onConnectionEvent);
}

// ---------------------------------------------------------------------------
// Active-agent events — full roles, throttled + authenticated-only.
// ---------------------------------------------------------------------------

export const AgentEventStreamContext = createContext<EventStreamContextValue | null>(null);

/** The detailed feed belongs exclusively to the selected, visible agent. */
export function AgentEventStreamProvider({ children }: { children: React.ReactNode }) {
  const activeId = sseRuntime.useStore((s) => s.activeId);
  const isVisible = sseRuntime.useDocumentVisible();
  const path = isVisible && activeId !== null
    ? `/api/system/all?agents=${activeId}`
    : null;
  const { subscribe } = useSseConnection(path, "systemAll", activeId, isVisible);

  return (
    <AgentEventStreamContext.Provider value={{ subscribe }}>
      {children}
    </AgentEventStreamContext.Provider>
  );
}

/**
 * Subscribe to the all-events throttled broadcast. Must be called inside
 * `<AgentEventStreamProvider>`. Same `onConnectionEvent`-required contract
 * as useEventStream. The stream carries the active agent plus system-level
 * events; consumers still filter internally via isEventForThread / role checks.
 */
export function useAgentEventStream(
  onSystemEvent: (event: SystemEvent) => void,
  onConnectionEvent: (ev: ConnectionEvent) => void,
  onSystemEventBatch?: (events: SystemEvent[]) => void,
): void {
  const ctx = useContext(AgentEventStreamContext);
  useSubscribeEffect(
    ctx,
    "useAgentEventStream",
    "AgentEventStreamProvider",
    onSystemEvent,
    onConnectionEvent,
    onSystemEventBatch,
  );
}
