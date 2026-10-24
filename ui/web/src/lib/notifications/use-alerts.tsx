"use client";

// Alerts live data — the Alert system's SSE provider + cache hooks
// (Task #1224). Alert is fully separate from Notice: own table, own UI, own
// IM channel.
//
// One profile-shared stream to /api/alerts/stream feeds the TanStack Query ["alerts"]
// prefix: every frame invalidates matching snapshots — no polling for
// SSE-backed data (frontend AGENTS.md state rule). The server owns filters,
// ordering and uncapped counts; a capped page cannot derive count deltas.
// The initial GET /api/alerts is the fetch fallback for rows
// ingested before the subscription opened.
//
// The connection lifecycle shares timers and auth probing with useEventStream;
// alert frame handling remains scoped to the one alert shape: frames that
// fail to parse are dropped, a wedged socket reopens, a CLOSED stream
// with a valid session reconnects with capped backoff, and an expired session
// stays closed. Hidden pages close the stream and reconcile their active reads on return.
//
// Cache shape: AlertsResponse (alerts + meta.unresolved_count). TanStack Query
// re-reads active observers and marks inactive history snapshots stale.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { useApi, useStreamHub } from "../transport/transport-context";
import { useAuth } from "../auth/auth-context";
import { createSseTimers, reopenIfClosed } from "../transport/sse-lifecycle";
import { useMergedSseChannels } from "../state/sse-merge";
import type { Alert, AlertsResponse } from "../contracts/types";
import { useDocumentVisible } from "../layout/use-document-visible";

// Half-dead-connection watchdog window (same value as useEventStream: the
// server emits a heartbeat data frame after ~15s of silence).
const WATCHDOG_MS = 45_000;
// SSE-connect-failure retry: EventSource auto-retries transient drops, but a
// non-2xx response leaves it CLOSED permanently. The session probe stops
// retries for 401/403; valid sessions reopen with capped exponential backoff.
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
/** The default-params cache key (badge / provider warm-up). */
export const ALERTS_QUERY_KEY = ["alerts"] as const;

/** The section's history-list cache key. */
export const ALERTS_SECTION_QUERY_KEY = ["alerts", "section"] as const;

/** A heartbeat frame from the stream — liveness only, never business data. */
function isHeartbeat(raw: unknown): boolean {
  return (
    typeof raw === "object" &&
    raw !== null &&
    !Array.isArray(raw) &&
    (raw as { role?: unknown }).role === "heartbeat"
  );
}

function isAlertRow(raw: unknown): raw is Alert {
  return (
    typeof raw === "object" &&
    raw !== null &&
    !Array.isArray(raw) &&
    typeof (raw as { id?: unknown }).id === "number" &&
    typeof (raw as { status?: unknown }).status === "string"
  );
}

export function AlertsProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const hub = useStreamHub();
  const queryClient = useQueryClient();
  const { status: authStatus } = useAuth();
  // The badge count stays warm for the whole app via this always-on
  // default-params query (the initial fetch fallback).
  useQuery<AlertsResponse>({
    queryKey: ALERTS_QUERY_KEY,
    queryFn: () => api.getAlerts({}),
    enabled: authStatus === "authenticated",
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const [reconnectNonce, setReconnectNonce] = useState(0);
  const [retryNonce, setRetryNonce] = useState(0);
  const failCountRef = useRef(0);
  const lastReconnectNonce = useRef(reconnectNonce);

  // Stable reconnect callback (mirrors useEventStream's bumpReconnect).
  const bumpReconnect = useCallback(() => setReconnectNonce((n) => n + 1), []);
  const isVisible = useDocumentVisible();
  const transport = hub.transportFor(useMergedSseChannels());

  useEffect(() => {
    if (authStatus !== "authenticated" || !isVisible) {
      // Session not authenticated: keep the stream closed (Task #1635 — an
      // unauthenticated SSE GET 401s and blind retries produced the 43k/24h
      // /api/alerts/stream storm). Login flips the status → effect re-runs → opens.
      // Hidden pages suspend work; reopening reconciles active alert reads.
      lastReconnectNonce.current = reconnectNonce;
      return;
    }

    let parseFailures = 0;
    const timers = createSseTimers({
      failCount: failCountRef,
      watchdogMs: WATCHDOG_MS,
      reconnectBaseMs: RECONNECT_BASE_MS,
      reconnectMaxMs: RECONNECT_MAX_MS,
    });
    const reopenLegacy = () => setRetryNonce((n) => n + 1);
    const armWatchdog = () => timers.armWatchdog(() => {
      if (transport) transport.restart("alerts");
      else bumpReconnect();
    });

    const reconcileFrame = (raw: string) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        parseFailures += 1;
        if (parseFailures >= 3) {
          parseFailures = 0;
          if (transport) {
            if (transport.isLeader()) transport.restart("alerts", true);
          } else timers.scheduleReopen(reopenLegacy);
        }
        return;
      }
      if (isHeartbeat(parsed) || !isAlertRow(parsed)) return;
      void queryClient.invalidateQueries({ queryKey: ALERTS_QUERY_KEY });
    };

    const handleOpen = () => {
      if (timers.isDisposed()) return;
      timers.resetBackoff();
      // Alerts have their own stream and staleTime: Infinity, so this provider
      // owns the precise reconnect repair for frames missed while disconnected.
      // Only active readers refetch; inactive history caches are marked stale.
      void queryClient.invalidateQueries({ queryKey: ALERTS_QUERY_KEY });
      armWatchdog();
    };
    const handleFrame = (raw: string) => {
      if (timers.isDisposed()) return;
      armWatchdog();
      if (!raw) return;
      reconcileFrame(raw);
    };
    if (transport) {
      const unsubscribe = transport.subscribe("alerts", {
        onFrame: handleFrame,
        onGap: () => handleOpen(),
        onState: (state) => {
          if (timers.isDisposed()) return;
          if (state === "open") handleOpen();
          else if (state === "closed") {
            timers.clearWatchdog();
          }
        },
      });
      if (lastReconnectNonce.current !== reconnectNonce) transport.restart("alerts");
      lastReconnectNonce.current = reconnectNonce;
      return () => {
        timers.dispose();
        unsubscribe();
      };
    }

    const es = hub.openLegacy("/api/alerts/stream");
    es.onopen = handleOpen;
    es.onmessage = (event) => handleFrame(typeof event.data === "string" ? event.data : "");
    es.onerror = () => {
      if (timers.isDisposed()) return;
      reopenIfClosed(es, hub.session, timers, reopenLegacy);
    };

    return () => {
      timers.dispose();
      es.close();
    };
    // reconnectNonce + retryNonce are the reopen levers; isVisible gates the
    // stream on tab visibility; the cache folders are stable identities
    // included only to satisfy the lint.
  }, [queryClient, reconnectNonce, retryNonce, bumpReconnect, authStatus, isVisible, transport, hub]);

  return children;
}

/** The badge count + rows (default params, warmed by the provider). */
export function useAlerts() {
  const api = useApi();
  return useQuery<AlertsResponse>({
    queryKey: ALERTS_QUERY_KEY,
    queryFn: () => api.getAlerts({}),
    staleTime: Infinity,
  });
}

/** The alert section's history. */
export function useAlertsSection() {
  const api = useApi();
  return useQuery({
    queryKey: ALERTS_SECTION_QUERY_KEY,
    // Pinned at 200 pending the task #3696 user-flag item (backend alerts
    // default alignment); the server default is display.alerts_default_limit.
    queryFn: () => api.getAlerts({ window: "24h", limit: 200 }),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}
