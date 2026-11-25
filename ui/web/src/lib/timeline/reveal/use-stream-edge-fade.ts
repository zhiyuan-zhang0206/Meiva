"use client";

// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md

import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

import { clearEdgeFade, paintEdgeFade, settleEdgeFade, type HighlightHost } from "./edge-fade";

export function useStreamEdgeFade(
  ref: RefObject<HTMLElement | null>,
  revealing: boolean,
  shown: string,
  host: HighlightHost | null,
): void {
  const [owner] = useState(() => ({}));
  const wasRevealing = useRef(false);

  useLayoutEffect(() => {
    const root = ref.current;
    if (!root || host === null) return;
    if (revealing) {
      wasRevealing.current = true;
      paintEdgeFade(host, owner, root);
      return;
    }
    if (!wasRevealing.current) return;
    wasRevealing.current = false;
    return settleEdgeFade(host, owner, root);
  }, [host, owner, ref, revealing, shown]);

  useEffect(() => () => clearEdgeFade(owner), [owner]);
}
