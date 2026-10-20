"use client";

import { useMemo } from "react";

import { POINTER_STICKY_THRESHOLDS, TOUCH_STICKY_THRESHOLDS } from "@/lib/layout/sticky";

// meiva:note
// The bottom zone width is pointer-dependent: a touch device gets the wide zone
// that absorbs iOS WebKit momentum and overscroll bounce, a wheel device a tight
// one so a small deliberate scroll-up escapes. Resolved once on mount, with the
// touch preset as the SSR default.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
export function useStickyThresholds() {
  return useMemo(
    () =>
      typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches
        ? TOUCH_STICKY_THRESHOLDS
        : POINTER_STICKY_THRESHOLDS,
    [],
  );
}
