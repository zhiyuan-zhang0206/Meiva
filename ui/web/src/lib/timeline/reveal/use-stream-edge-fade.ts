"use client";

// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md

import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

import { clearEdgeFade, domHighlightHost, paintEdgeFade, settleEdgeFade, type HighlightHost } from "./edge-fade";

export const EdgeFadeHostContext = createContext<HighlightHost | null | undefined>(undefined);

export function useEdgeFadeHost(): HighlightHost | null {
  const provided = useContext(EdgeFadeHostContext);
  return provided === undefined ? domHighlightHost() : provided;
}

export function useStreamEdgeFade(
  ref: RefObject<HTMLElement | null>,
  caughtUp: boolean,
  shown: string,
  host: HighlightHost | null,
): void {
  const [owner] = useState(() => ({}));
  const wasPainting = useRef(false);

  useLayoutEffect(() => {
    const root = ref.current;
    if (!root || host === null) {
      clearEdgeFade(owner);
      return;
    }
    if (!caughtUp) {
      wasPainting.current = true;
      paintEdgeFade(host, owner, root);
      return;
    }
    if (!wasPainting.current) return;
    wasPainting.current = false;
    return settleEdgeFade(host, owner, root);
  }, [host, owner, ref, caughtUp, shown]);

  useEffect(() => () => clearEdgeFade(owner), [owner]);
}
