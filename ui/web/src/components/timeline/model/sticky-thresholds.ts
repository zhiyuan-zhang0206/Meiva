"use client";

import { useMemo } from "react";

import { useMediaQuery } from "@/lib/layout/use-media-query";
import { POINTER_STICKY_THRESHOLDS, TOUCH_STICKY_THRESHOLDS } from "@/lib/layout/sticky";

// meiva:note
// The down arrow's visibility zone is wider on touch to cover momentum and overscroll bounce.
// The server snapshot uses the touch preset, then the client reads its pointer media query.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function useStickyThresholds() {
  const coarse = useMediaQuery("(pointer: coarse)");
  return useMemo(
    () => (coarse ? TOUCH_STICKY_THRESHOLDS : POINTER_STICKY_THRESHOLDS),
    [coarse],
  );
}
