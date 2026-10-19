import type { RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import type { StickyController } from "@/lib/layout/sticky";

import { settleSavedTimelineScroll } from "@/components/timeline/model/restore-settle";
import { applySavedTimelineScroll } from "@/components/timeline/model/use-timeline-window";

// meiva:note
// A bookmark restore owns the viewport until it lands (applyBookmarkRestore), keeps the layout from pinning meanwhile (the hold), and then verifies its own target on later layout passes (settleRestoredPosition). Two escapes keep a bookmark from owning the viewport forever: a bookmark past the end of a page that stopped growing lands on the end, and a settle that reaches its target releases the hold so following and the position save resume.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md

export interface BookmarkState {
  pending: RefObject<SavedScroll | null>;
  settled: RefObject<SavedScroll | null>;
  hold: RefObject<boolean>;
  // meiva:note
  // Consecutive layout passes at one content height: a bookmark beyond the reachable end is only unreachable once the content stopped growing.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
  probe: RefObject<{ height: number; passes: number }>;
}

interface ApplyArgs {
  state: BookmarkState;
  viewport: HTMLElement | null;
  initialPass: boolean;
  threadKey: string | undefined;
  compactBuffer: CompactTransitionBuffer | null;
  canonicalItems: readonly BackendTimelineItem[];
  controller: StickyController;
}

const snapshot = (viewport: HTMLElement) => ({
  scrollTop: viewport.scrollTop,
  scrollHeight: viewport.scrollHeight,
  clientHeight: viewport.clientHeight,
});

export function applyBookmarkRestore({
  state, viewport, initialPass, threadKey, compactBuffer, canonicalItems, controller,
}: ApplyArgs): void {
  const saved = state.pending.current;
  if (initialPass || !saved || !viewport) return;
  if (threadKey === undefined) return;
  if (saved.contentKey !== threadKey) {
    state.pending.current = null;
    state.hold.current = false;
    return;
  }
  if (!applySavedTimelineScroll(viewport, saved, compactBuffer, canonicalItems)) {
    const max = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    const probe = state.probe.current;
    if (probe.passes >= 2 && probe.height === viewport.scrollHeight && saved.scrollTop > max) {
      // meiva:note
      // Older rows were trimmed while away: the bookmark lies past the end of a page that stopped growing. Waiting cannot reach it - land on the end and hand the viewport back, or the hold silences following and every later send for good.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
      viewport.scrollTop = max;
      controller.notifyRestored(snapshot(viewport));
      state.pending.current = null;
      state.settled.current = null;
      state.hold.current = false;
      return;
    }
    controller.notifyRestored(snapshot(viewport));
    return;
  }
  controller.notifyRestored(snapshot(viewport));
  state.pending.current = null;
  state.settled.current = saved.anchor ? saved : null;
  if (!saved.anchor) state.hold.current = false;
}

interface SettleArgs {
  state: BookmarkState;
  viewport: HTMLElement | null;
  layoutChanged: boolean;
  threadKey: string | undefined;
  compactBuffer: CompactTransitionBuffer | null;
  canonicalItems: readonly BackendTimelineItem[];
}

export function settleRestoredPosition({
  state, viewport, layoutChanged, threadKey, compactBuffer, canonicalItems,
}: SettleArgs): void {
  const saved = state.settled.current;
  if (!saved || !viewport) return;
  if (state.pending.current !== null || !state.hold.current || threadKey === undefined || saved.contentKey !== threadKey) {
    state.settled.current = null;
    return;
  }
  if (layoutChanged && settleSavedTimelineScroll(viewport, saved, compactBuffer, canonicalItems)) {
    state.settled.current = null;
    state.hold.current = false;
  }
}
