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
// answer. Deferring them past the first paint is not enough on its own: they
// then all land in the same idle callback, which is one commit - and one style
// and layout pass - over every node at once. Control still froze for 1.2 s at
// 8x CPU throttle that way, and its config form alone is 6.6k nodes of 380
// rows. So waiting subtrees queue up and the queue releases ONE per idle slice:
// the route commits at once, and the page fills in over frames instead of
// holding the main thread for seconds (user 2026-10-20: opening Settings stayed
// frozen for ~5 s on their machine).
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
export const DEFERRED_MOUNT_SLICE_TIMEOUT_MS = 50;

const waiting: (() => void)[] = [];
let pumpScheduled = false;

function releaseOne(): void {
  pumpScheduled = false;
  const next = waiting.shift();
  if (next === undefined) return;
  next();
  schedulePump();
}

function schedulePump(): void {
  if (pumpScheduled || waiting.length === 0) return;
  pumpScheduled = true;
  if (typeof requestIdleCallback === "function") {
    requestIdleCallback(releaseOne, { timeout: DEFERRED_MOUNT_SLICE_TIMEOUT_MS });
  } else {
    setTimeout(releaseOne, 0);
  }
}

function enqueueMount(release: () => void): () => void {
  waiting.push(release);
  schedulePump();
  return () => {
    const at = waiting.indexOf(release);
    if (at >= 0) waiting.splice(at, 1);
  };
}

export function useDeferredMount(): boolean {
  const [mounted, setMounted] = useState(() =>
    typeof window !== "undefined" && window.location.hash.length > 1,
  );
  useEffect(() => {
    let cancelled = false;
    let drop: (() => void) | null = null;
    const mount = () => {
      if (!cancelled) setMounted(true);
    };
    const frame = requestAnimationFrame(() => {
      if (!cancelled) drop = enqueueMount(mount);
    });
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
      drop?.();
    };
  }, []);
  return mounted;
}
