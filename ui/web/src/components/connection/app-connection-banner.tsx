"use client";

// Authenticated host-status polling and global SSE connection tracking.
import { useCallback, useEffect, useRef } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { CONNECTION_ERROR_DELAY_MS } from "@/components/notifications/connection-notice";
import { useStore } from "@/lib/state/store";
import { useClusterHealth } from "@/lib/notifications/use-cluster-health";
import type { ConnectionEvent } from "@/lib/transport/useEventStream";
import { useEventStream } from "@/lib/transport/useEventStream";

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

  // Poll cluster status + drive the reconnect-on-update-done edge. Single mount.
  useClusterHealth();

  useEffect(() => {
    return () => {
      if (pendingRef.current !== null) clearTimeout(pendingRef.current);
      pendingRef.current = null;
    };
  }, []);

  // meiva:note
  // The store's connection state is what the user is told, not raw transport events.
  // Transient drops (reconnecting or closed) auto-retry silently via backoff;
  // publishing them immediately produced annoying yellow flashing.
  // Drops lasting >= CONNECTION_ERROR_DELAY_MS (15s) escalate directly to "closed",
  // which surfaces the floating red error notice. An "open" event immediately
  // cancels any pending escalation and restores "open".
  // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
  const onSystemEvent = useCallback(() => undefined, []);
  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      if (ev.type === "parse-failed") return;
      if (ev.type === "reconnecting" || ev.type === "closed") {
        if (useStore.getState().connState === "closed") return;
        if (pendingRef.current !== null) return;
        pendingRef.current = setTimeout(() => {
          pendingRef.current = null;
          setConnState("closed");
        }, CONNECTION_ERROR_DELAY_MS);
        return;
      }
      if (pendingRef.current !== null) {
        clearTimeout(pendingRef.current);
        pendingRef.current = null;
      }
      setConnState("open");
    },
    [setConnState],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  return null;
}
