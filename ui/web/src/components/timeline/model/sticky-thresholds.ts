"use client";

import { useMemo } from "react";

import { useMediaQuery } from "@/lib/layout/use-media-query";
import { POINTER_STICKY_THRESHOLDS, TOUCH_STICKY_THRESHOLDS } from "@/lib/layout/sticky";

// meiva:note
// The bottom zone width is pointer-dependent: a touch device gets the wide zone
// that absorbs iOS WebKit momentum and overscroll bounce, a wheel device a tight
// one so a small deliberate scroll-up escapes. The answer comes from the media
// query hook, so a server render uses its snapshot (the touch preset) and a
// client mount reads the real pointer in its first render.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
export function useStickyThresholds() {
  const coarse = useMediaQuery("(pointer: coarse)");
  return useMemo(
    () => (coarse ? TOUCH_STICKY_THRESHOLDS : POINTER_STICKY_THRESHOLDS),
    [coarse],
  );
}
