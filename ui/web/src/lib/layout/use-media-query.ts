"use client";

import { useCallback, useMemo, useSyncExternalStore } from "react";

import { useBrowser } from "@/lib/browser/browser-provider";

/** Stable server snapshot — queried during SSR and the hydrating render. */
const serverSnapshot = () => false;

// meiva:note
// SSR-safe media query hook. The match is read through `useSyncExternalStore`: on the client the
// REAL match is available in the first render (a client-side navigation renders the real frame in
// its first commit — no post-mount flip), while SSR and hydration use the server snapshot
// (`false`, the mobile-first frame) and re-render once the client store answers. A desktop load
// therefore still paints the mobile frame until hydration completes — the accepted contract,
// condensed in `useBreakpoint`'s note.
//
// The query is answered by the browser port (`useBrowser().media`), so a test answers it by value
// — `fakeBrowser({ media: { "(min-width: 768px)": true } })` — instead of resizing a DOM window. A
// server render never asks: it renders the snapshot above, and the port says so if anything
// insists. The value used to live in `useState` + `useEffect`, where every client-side mount
// committed the mobile-first frame first and corrected it after the passive flush — a painted
// wrong frame whenever a commit and the flush straddled a rendering pass.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
export function useMediaQuery(query: string): boolean {
  const browser = useBrowser();
  const asked = useMemo(() => browser.media.query(query), [browser, query]);
  const subscribe = useCallback(
    (onStoreChange: () => void) => asked.subscribe(onStoreChange),
    [asked],
  );
  const getSnapshot = useCallback(() => asked.matches(), [asked]);
  return useSyncExternalStore(subscribe, getSnapshot, serverSnapshot);
}
