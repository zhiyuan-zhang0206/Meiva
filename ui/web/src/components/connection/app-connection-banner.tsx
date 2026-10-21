"use client";

// Authenticated host-status polling and global SSE connection tracking.
import { useCallback, useEffect, useRef } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { NOTICE_DELAY_MS } from "@/components/notifications/connection-notice";
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
  // The store's connection state is what the user is told, not what the socket did.
  // A merged-stream reconnect (every agent switch re-opens it) is a few hundred
  // milliseconds; publishing it immediately made the live-update banner flash and the
  // timeline jump under it. "open" and "closed" land at once — one is recovery, the
  // other is a socket that will not retry — while "reconnecting" waits out
  // NOTICE_DELAY_MS and disappears without ever having been announced if it recovers.
  // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
  const onSystemEvent = useCallback(() => undefined, []);
  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      if (ev.type === "parse-failed") return;
      if (ev.type === "reconnecting") {
        if (pendingRef.current !== null) return;
        pendingRef.current = setTimeout(() => {
          pendingRef.current = null;
          setConnState("reconnecting");
        }, NOTICE_DELAY_MS);
        return;
      }
      if (pendingRef.current !== null) {
        clearTimeout(pendingRef.current);
        pendingRef.current = null;
      }
      setConnState(ev.type);
    },
    [setConnState],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  return null;
}
