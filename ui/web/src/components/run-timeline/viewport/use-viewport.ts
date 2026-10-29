"use client";

// meiva:note
// A component that must follow the viewport — a tick label, an arrow, the row of link ticks — re-renders
// when the store moves. That is cheap and bounded by how many labels there are, unlike the row tree,
// whose canvases paint without React (task #5738).
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md

import { useCallback, useSyncExternalStore } from "react";

import type { Viewport } from "../model/timeline-model-viewport";
import type { ViewportStore } from "./viewport-store";
import { countFollowerRender } from "../model/render-stats";

export function useViewport(store: ViewportStore): Viewport {
  countFollowerRender();
  const subscribe = useCallback((onChange: () => void) => store.subscribe(onChange), [store]);
  const current = useCallback(() => store.get(), [store]);
  return useSyncExternalStore(subscribe, current, current);
}
