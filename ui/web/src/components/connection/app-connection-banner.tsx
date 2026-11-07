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
  const pendingRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { page } = useStreamHub().transport;
  const visibleRef = useRef<boolean | null>(null);

  // Poll cluster status + drive the reconnect-on-update-done edge. Single mount.
  useClusterHealth();

  const clearPending = useCallback(() => {
    if (pendingRef.current !== null) clearTimeout(pendingRef.current);
    pendingRef.current = null;
  }, []);

  const startRecovery = useCallback(() => {
    if (pendingRef.current !== null) return;
    setConnState("reconnecting");
    pendingRef.current = setTimeout(() => {
      pendingRef.current = null;
      if (page.visibilityState === "visible") setConnState("closed");
    }, CONNECTION_ERROR_DELAY_MS);
  }, [page, setConnState]);

  // meiva:note
  // Hidden pages deliberately leave SSE. Give each visible participation a fresh
  // recovery window, including an initial connection that never emits an error.
  // Repeated transport errors retain the same deadline; only open or hide ends it.
  // meiva:link ui/web/src/docs/provider-composition.meiva.okf.md
  const syncVisibility = useCallback(() => {
    const visible = page.visibilityState === "visible";
    if (visible === visibleRef.current) return;
    visibleRef.current = visible;
    clearPending();
    if (visible) startRecovery();
    else setConnState("reconnecting");
  }, [page, clearPending, startRecovery, setConnState]);

  useEffect(() => {
    syncVisibility();
    page.addEventListener("visibilitychange", syncVisibility);
    return () => {
      clearPending();
      visibleRef.current = null;
      page.removeEventListener("visibilitychange", syncVisibility);
    };
  }, [page, clearPending, syncVisibility]);

  const onSystemEvent = useCallback(() => undefined, []);
  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      syncVisibility();
      if (page.visibilityState !== "visible" || ev.type === "parse-failed") return;
      if (ev.type === "reconnecting" || ev.type === "closed") {
        if (useStore.getState().connState !== "closed") startRecovery();
        return;
      }
      clearPending();
      setConnState("open");
    },
    [page, clearPending, startRecovery, setConnState, syncVisibility],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  return null;
}
