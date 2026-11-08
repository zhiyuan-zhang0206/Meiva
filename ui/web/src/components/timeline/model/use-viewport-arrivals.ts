import { useCallback, useLayoutEffect, useRef, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { SEND_ANCHOR_TOP_SHARE, type StickyController } from "@/lib/layout/sticky";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { useTimelineStore } from "@/lib/timeline/timeline-store";

// meiva:note
// Where the viewport goes when the READER asks for something, rather than when
// the stream grows: a thread change pins to the bottom, a send parks the
// message that was just written. Both are placements the follow flag has to
// learn about, and both release a bookmark that was still settling - the reader
// picked a position, so the saved one no longer owns the viewport. The pin is
// immediate; the anchor waits for its row to mount, because until then there is
// nothing to park.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md

export interface ThreadPinArgs {
  threadKey: string | undefined;
  controller: StickyController;
  viewportRef: RefObject<HTMLElement | null>;
  wrapperRef: RefObject<HTMLDivElement | null>;
  anchorRef: RefObject<{ id: string; rank: number; frontId: string | null; docTop: number } | null>;
  pendingRef: RefObject<SavedScroll | null>;
  settledRef: RefObject<SavedScroll | null>;
  holdRef: RefObject<boolean>;
  pinToBottom: (viewport: HTMLElement, freshRun?: boolean) => void;
  setAtBottom: (value: boolean) => void;
}

export function useThreadPin({
  threadKey, viewportRef, wrapperRef, anchorRef,
  pendingRef, settledRef, holdRef, pinToBottom, setAtBottom,
}: ThreadPinArgs): void {
  const last = useRef<{ thread: string | undefined } | null>(null);
  useLayoutEffect(() => {
    const viewport = viewportRef.current
      ?? wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')
      ?? null;
    const previous = last.current;
    if (previous !== null && previous.thread === threadKey) return;
    const pending = pendingRef.current;
    if (pending !== null && pending.contentKey === threadKey) {
      last.current = { thread: threadKey };
      return;
    }
    last.current = { thread: threadKey };
    anchorRef.current = null;
    pendingRef.current = null;
    settledRef.current = null;
    holdRef.current = false;
    setAtBottom(true);
    if (viewport) pinToBottom(viewport, true);
  }, [pinToBottom, setAtBottom, threadKey, viewportRef, wrapperRef, anchorRef, pendingRef, settledRef, holdRef]);
}

// meiva:note
// The send anchor: once the reader's own message is committed and mounted, park
// that row a fixed share down the viewport and let following go, so the reply
// streams into the space below it instead of dragging the reader down chunk by
// chunk (user request 2026-11-09). A bottom reserve makes the parking position
// reachable while the reply is still shorter than the viewport: it fills exactly
// the missing height and gives it back as the reply grows, so scrollHeight - and
// with it the reader's viewport - stays where it is. The controller owns the
// follow flag; this hook only writes positions and reports them.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
export interface SendAnchorArgs {
  items: readonly BackendTimelineItem[];
  viewportRef: RefObject<HTMLElement | null>;
  wrapperRef: RefObject<HTMLDivElement | null>;
  reserveRef: RefObject<HTMLDivElement | null>;
  controller: StickyController;
  measureAtBottom: () => void;
  cancelBookmark: () => void;
}

function snapshotOf(viewport: HTMLElement) {
  return {
    scrollTop: viewport.scrollTop,
    scrollHeight: viewport.scrollHeight,
    clientHeight: viewport.clientHeight,
  };
}

function reserveHeight(element: HTMLElement): number {
  return element.offsetHeight;
}

// meiva:note
// Which committed row a send anchor belongs to: the inbound the receipt named,
// or, when the receipt carried none, the newest user message the timeline shows.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
function targetItemId(
  items: readonly BackendTimelineItem[],
  inboundId: number | null,
): string | null {
  if (inboundId !== null) {
    return items.find((item) => item.inbound_id === inboundId)?.item_id ?? null;
  }
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (items[index].kind === "inbound_chat") return items[index].item_id;
  }
  return null;
}

function findRow(viewport: HTMLElement, itemId: string): HTMLElement | null {
  const escaped = CSS.escape(itemId);
  return viewport.querySelector<HTMLElement>('.timeline-item[data-item-id="' + escaped + '"]')
    ?? viewport.querySelector<HTMLElement>('[data-turn-expanded="false"][data-item-id="' + escaped + '"]');
}

export function useSendAnchor({
  items,
  viewportRef,
  wrapperRef,
  reserveRef,
  controller,
  measureAtBottom,
  cancelBookmark,
}: SendAnchorArgs): { maintain: () => void } {
  const request = useTimelineStore((state) => state.sendAnchor);
  // meiva:note
  // One anchor is three pieces of state: the sequence already consumed (a
  // request recorded before this view mounted belongs to a thread it never
  // showed, so it is not owed here), the inbound still waiting for its row
  // (`undefined` = nothing owed, `null` = the newest user message stands in),
  // and the position being held (null while no reserve is outstanding).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
  const consumedSeq = useRef(request.seq);
  const pending = useRef<number | null | undefined>(undefined);
  const anchoredTop = useRef<number | null>(null);

  const writeReserve = useCallback((height: number) => {
    const element = reserveRef.current;
    if (!element) return;
    const next = Math.max(0, Math.ceil(height));
    if (Math.abs(reserveHeight(element) - next) <= 1) return;
    element.style.height = `${next}px`;
  }, [reserveRef]);

  const maintain = useCallback(() => {
    const viewport = viewportRef.current;
    const element = reserveRef.current;
    const top = anchoredTop.current;
    if (!viewport || !element || top === null) return;
    if (controller.isSticky()) {
      // meiva:note
      // Following again: the reserve no longer holds the reader, and keeping it
      // would park blank space between them and the tail.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
      anchoredTop.current = null;
      writeReserve(0);
      return;
    }
    writeReserve(top + viewport.clientHeight - (viewport.scrollHeight - reserveHeight(element)));
  }, [controller, reserveRef, viewportRef, writeReserve]);

  const anchorRow = useCallback((viewport: HTMLElement, node: HTMLElement) => {
    // meiva:note
    // The reader asked for the message they just wrote, so a bookmark still
    // settling no longer owns the viewport. Released here rather than when the
    // request arrives: until the row mounts there is nothing to park, and a
    // bookmark that lands first is still the reader's place.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
    cancelBookmark();
    const reserve = reserveRef.current;
    const rowTop = node.getBoundingClientRect().top - viewport.getBoundingClientRect().top + viewport.scrollTop;
    const top = Math.max(0, Math.round(rowTop - viewport.clientHeight * SEND_ANCHOR_TOP_SHARE));
    const natural = viewport.scrollHeight - (reserve ? reserveHeight(reserve) : 0);
    writeReserve(top + viewport.clientHeight - natural);
    viewport.scrollTop = top;
    pending.current = undefined;
    // meiva:note
    // The reserved height makes `top` reachable, so the browser keeps it; a
    // clamp would become the held position instead of a fight with the layout.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
    anchoredTop.current = viewport.scrollTop;
    controller.notifyAnchored(snapshotOf(viewport));
    measureAtBottom();
  }, [cancelBookmark, controller, measureAtBottom, reserveRef, writeReserve]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current
      ?? wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')
      ?? null;
    if (!viewport) return;

    if (request.seq !== consumedSeq.current) {
      consumedSeq.current = request.seq;
      pending.current = request.inboundId;
      anchoredTop.current = null;
      writeReserve(0);
    }

    if (pending.current !== undefined && anchoredTop.current === null) {
      const itemId = targetItemId(items, pending.current);
      const node = itemId === null ? null : findRow(viewport, itemId);
      if (node) anchorRow(viewport, node);
    }
    maintain();
  });

  return { maintain };
}
