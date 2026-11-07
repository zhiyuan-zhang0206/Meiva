"use client";

// Authenticated host-status polling and global SSE connection tracking.
import { useCallback, useEffect, useRef } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { CONNECTION_ERROR_DELAY_MS } from "@/components/notifications/connection-notice";
import { useStore } from "@/lib/state/store";
import { useClusterHealth } from "@/lib/notifications/use-cluster-health";
import type { ConnectionEvent } from "@/lib/transport/useEventStream";
import { useEventStream } from "@/lib/transport/useEventStream";
import { useStreamHub } from "@/lib/transport/transport-context";

export function AppConnectionBanner() {
  const { status } = useAuth();
  // Conditional MOUNT (not conditional hooks): the pollers + SSE subscription only
  // come alive once authenticated, so nothing 401s behind the login screen.
  if (status !== "authenticated") return null;
  return <ConnectionHealthProvider />;
}

function ConnectionHealthProvider() {
  const setConnState = useStore((s) => s.setConnState);
  const bumpReconnect = useStore((s) => s.bumpReconnect);
  const pendingRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { page } = useStreamHub().transport;
  const foregroundRef = useRef<boolean | null>(null);

  // Poll cluster status + drive the reconnect-on-update-done edge. Single mount.
  useClusterHealth();

  const clearPending = useCallback(() => {
    if (pendingRef.current !== null) clearTimeout(pendingRef.current);
    pendingRef.current = null;
  }, []);

  const isForeground = useCallback(
    () => page.visibilityState === "visible" && page.hasFocus(),
    [page],
  );

  const startRecovery = useCallback(() => {
    if (pendingRef.current !== null) return;
    setConnState("reconnecting");
    pendingRef.current = setTimeout(() => {
      pendingRef.current = null;
      if (isForeground()) setConnState("closed");
    }, CONNECTION_ERROR_DELAY_MS);
  }, [isForeground, setConnState]);

  // meiva:note
  // Hidden pages leave SSE; an unfocused but visible WebView may keep it alive.
  // Count failure only in the foreground, using the transport's own document.
  // Repeated errors retain one deadline. Returning to a failed visible stream
  // requests immediate repair; a healthy stream survives focus changes untouched.
  // meiva:link ui/web/src/docs/provider-composition.meiva.okf.md
  const syncForeground = useCallback(() => {
    const foreground = isForeground();
    const previous = foregroundRef.current;
    if (foreground === previous) return;
    foregroundRef.current = foreground;
    clearPending();
    const state = useStore.getState().connState;
    if (foreground) {
      if (previous === null || state !== "open") startRecovery();
    } else if (previous === null || page.visibilityState !== "visible" || state === "closed") {
      setConnState("reconnecting");
    }
  }, [page, isForeground, clearPending, startRecovery, setConnState]);

  useEffect(() => {
    syncForeground();
    const onFocus = () => {
      const wasForeground = foregroundRef.current;
      syncForeground();
      if (!wasForeground && isForeground() && useStore.getState().connState !== "open") {
        bumpReconnect();
      }
    };
    page.addEventListener("visibilitychange", syncForeground);
    page.defaultView?.addEventListener("blur", syncForeground);
    page.defaultView?.addEventListener("focus", onFocus);
    return () => {
      clearPending();
      foregroundRef.current = null;
      page.removeEventListener("visibilitychange", syncForeground);
      page.defaultView?.removeEventListener("blur", syncForeground);
      page.defaultView?.removeEventListener("focus", onFocus);
    };
  }, [page, clearPending, syncForeground, isForeground, bumpReconnect]);

  const onSystemEvent = useCallback(() => undefined, []);
  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      syncForeground();
      if (page.visibilityState !== "visible" || ev.type === "parse-failed") return;
      if (ev.type === "reconnecting" || ev.type === "closed") {
        if (!isForeground()) setConnState("reconnecting");
        else if (useStore.getState().connState !== "closed") startRecovery();
        return;
      }
      clearPending();
      setConnState("open");
    },
    [page, isForeground, clearPending, startRecovery, setConnState, syncForeground],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  return null;
}
