"use client";

import { useLayoutEffect, useRef, type RefObject } from "react";

// meiva:note
// Pin active Silver Glint shimmer animations to document timeline origin (startTime = 0)
// so sidebar rows and timeline working headers sweep in phase across mounts and handoffs
// (e.g. PendingLiveActivityWindow -> TurnBlock) without restarting mid-cycle. With the sweep
// sized to each title (globals.css), every working title crosses its own text at the same moment.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
export function useSyncedShimmer<T extends HTMLElement = HTMLSpanElement>(
  active: boolean,
): RefObject<T | null> {
  const ref = useRef<T | null>(null);
  useLayoutEffect(() => {
    if (!active) return;
    const el = ref.current;
    if (!el || typeof el.getAnimations !== "function") return;
    for (const anim of el.getAnimations()) {
      anim.startTime = 0;
    }
  }, [active]);
  return ref;
}
