"use client";

import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { readScrollMemory } from "@/lib/layout/scroll-memory";
import type { StickyController } from "@/lib/layout/sticky";

import { applySavedTimelineScroll } from "./use-timeline-window";

// meiva:note
// The viewport is queried from the scroll area wrapper, as the timeline's own
// effects do; the entry key is the one this timeline already saves its bookmark
// under, so the anchor being re-applied is the reader's own.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
interface Args {
  wrapperRef: RefObject<HTMLDivElement | null>;
  entriesRef: RefObject<{ entryKey: string; contentKey: string } | null>;
  holdRef: RefObject<boolean>;
  controller: StickyController;
  compactBuffer: CompactTransitionBuffer | null;
  canonicalItems: readonly BackendTimelineItem[];
}

// meiva:note
// A viewport WIDTH change re-wraps every row, so the pixel offset that was the
// reader's place no longer points at the row they were reading — opening or
// closing the sidebar moves this column by a sixth of the window with nothing
// about the content changed. The bookmark's anchor survives that: re-apply it
// after the reflow, and leave a reader in the bottom zone to the pin.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
export function useReanchorOnWidthChange({
  wrapperRef, entriesRef, holdRef, controller, compactBuffer, canonicalItems,
}: Args): void {
  const latest = useRef({ entriesRef, holdRef, controller, compactBuffer, canonicalItems });
  useLayoutEffect(() => {
    latest.current = { entriesRef, holdRef, controller, compactBuffer, canonicalItems };
  });

  useEffect(() => {
    const viewport = wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
    if (!viewport) return;
    let width = viewport.clientWidth;
    const observer = new ResizeObserver(() => {
      const next = viewport.clientWidth;
      if (next === width) return;
      width = next;
      const current = latest.current;
      const mem = current.entriesRef.current;
      if (!mem || current.holdRef.current || current.controller.isSticky()) return;
      const saved = readScrollMemory(mem.entryKey);
      if (!saved || saved.followBottom || saved.contentKey !== mem.contentKey) return;
      applySavedTimelineScroll(viewport, saved, current.compactBuffer, current.canonicalItems);
    });
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [wrapperRef]);
}
