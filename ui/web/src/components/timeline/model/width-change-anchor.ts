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
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
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
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
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
    const reapplyBookmark = (requireAnchor = false) => {
      const current = latest.current;
      const mem = current.entriesRef.current;
      if (!mem || current.holdRef.current || current.controller.isSticky()) return;
      const saved = readScrollMemory(mem.entryKey);
      if (!saved || saved.followBottom || saved.contentKey !== mem.contentKey || (requireAnchor && !saved.anchor)) return;
      applySavedTimelineScroll(viewport, saved, current.compactBuffer, current.canonicalItems);
    };
    const observer = new ResizeObserver(() => {
      const next = viewport.clientWidth;
      if (next === width) return;
      width = next;
      reapplyBookmark();
    });
    // meiva:note
    // Short histories do not activate row measurement. Decode can still move the
    // reader's row. Reuse their latest bookmark after the image and any failure
    // fallback commit; reader navigation updates that bookmark while waiting.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
    let mediaFrame = 0;
    const onMedia = (event: Event) => {
      if (!(event.target instanceof HTMLImageElement)) return;
      cancelAnimationFrame(mediaFrame);
      mediaFrame = requestAnimationFrame(() => reapplyBookmark(true));
    };
    viewport.addEventListener("load", onMedia, true);
    viewport.addEventListener("error", onMedia, true);
    observer.observe(viewport);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(mediaFrame);
      viewport.removeEventListener("load", onMedia, true);
      viewport.removeEventListener("error", onMedia, true);
    };
  }, [wrapperRef]);
}
