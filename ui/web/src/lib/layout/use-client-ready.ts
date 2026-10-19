"use client";

// meiva:note
// This module owns client-side mount timing: whether a frame is shaped by the
// server snapshot or the client (useClientReady), and when a deferred subtree
// may mount (useDeferredMount).
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md

import { useEffect, useState, useSyncExternalStore } from "react";

/** Nothing ever changes — the "store" is the client's own existence. */
const never = () => () => {
  // No subscription to release.
};

/**
 * False through SSR and the hydrating render, then true; TRUE already on the
 * first render of a client-side mount. The sanctioned `useSyncExternalStore`
 * server-snapshot pattern for "is this frame shaped by the server snapshot or
 * by the client?".
 *
 * The home layout uses it as its placeholder gate: a panel group must not
 * register before the breakpoint values are the client's own (registering
 * the SSR-safe mobile frame on a hydrating desktop load would remount the
 * group — the note on `HomeLayout`). A `useEffect`-flipped `mounted` flag
 * answered "not yet" on client-side navigations too, so every back/forward
 * into the home page painted one blank placeholder frame; keying the gate on
 * the snapshot's origin removes that frame without touching the hydration
 * contract.
 */
export function useClientReady(): boolean {
  return useSyncExternalStore(never, () => true, () => false);
}

// meiva:note
// Mount timing for subtrees that are expensive but not part of the route's
// answer. A page that mounts every section body in the same transition pays for
// all of it before the router commits the URL: Control added 13,878 DOM nodes in
// one transition and its URL did not change for 1.36 s on a 4x-slower CPU (user
// report 2026-10-20). DEFERRED_MOUNT_DEADLINE_MS is a hard ceiling, so a page
// that never reaches idle still mounts its bodies instead of hanging. A URL that
// carries a hash starts already mounted: it lands on a section and lets
// useSettledAnchorScroll settle the jump while the content above grows, so that
// path keeps the growth schedule it always had.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
export const DEFERRED_MOUNT_DEADLINE_MS = 200;

export function useDeferredMount(deadlineMs: number = DEFERRED_MOUNT_DEADLINE_MS): boolean {
  const [mounted, setMounted] = useState(() =>
    typeof window !== "undefined" && window.location.hash.length > 1,
  );
  useEffect(() => {
    let cancelled = false;
    let idle: number | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const mount = () => {
      if (!cancelled) setMounted(true);
    };
    const frame = requestAnimationFrame(() => {
      if (cancelled) return;
      if (typeof requestIdleCallback === "function") {
        idle = requestIdleCallback(mount, { timeout: deadlineMs });
      } else {
        timer = setTimeout(mount, 0);
      }
    });
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
      if (idle !== null) cancelIdleCallback(idle);
      if (timer !== null) clearTimeout(timer);
    };
  }, [deadlineMs]);
  return mounted;
}
