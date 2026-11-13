import { useCallback, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { StickyController } from "@/lib/layout/sticky";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import { parseItemIdParts } from "@/lib/timeline/timeline";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { useTimelineStore } from "@/lib/timeline/timeline-store";

import { materializeBeforeAnchor } from "./materialize-prefix";
import { inboundKind, isSilentTimelineItem } from "./runs";

// meiva:note
// Where the viewport goes when the READER asks for something, rather than when
// the stream grows: a thread change pins to the bottom, a send parks the
// message that was just written at the top of the viewport. Both
// are placements the follow flag has to learn about, and both release a
// bookmark that was still settling - the reader picked a position, so the saved
// one no longer owns the viewport.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

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

export interface ReadingTarget {
  id: string;
  rank: number;
}

export interface SendAnchorArgs {
  items: readonly BackendTimelineItem[];
  compactBuffer: CompactTransitionBuffer | null;
  threadKey: string | undefined;
  viewportRef: RefObject<HTMLElement | null>;
  wrapperRef: RefObject<HTMLDivElement | null>;
  reserveRef: RefObject<HTMLDivElement | null>;
  controller: StickyController;
  isFollowing: () => boolean;
  topOffset: number;
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

export function parkScrollTop(rowDocTop: number, topOffset: number): number {
  return Math.max(0, Math.round(rowDocTop - topOffset));
}

export function parkReserve(top: number, clientHeight: number, naturalHeight: number): number {
  return Math.max(0, top + clientHeight - naturalHeight);
}

function isHumanMessage(item: BackendTimelineItem): boolean {
  return item.kind === "inbound_chat" && inboundKind(item.source) === "human" && !isSilentTimelineItem(item);
}

function rowFor(viewport: HTMLElement, target: ReadingTarget): HTMLElement | null {
  return viewport.querySelector<HTMLElement>(
    `[data-message-role="user"][data-item-id="${CSS.escape(target.id)}"][data-display-rank="${target.rank}"]`,
  );
}

function offsetInViewport(viewport: HTMLElement, node: HTMLElement): number {
  return node.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
}

// meiva:note
// The reserve belongs to the latest human row, even while the reader is in
// history. Its leading edge gives the real content height independently of
// the viewport's minimum scrollHeight and of any previous reserve.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
export function useSendAnchor({
  items, compactBuffer, threadKey, viewportRef, wrapperRef, reserveRef, controller,
  isFollowing, topOffset, measureAtBottom, cancelBookmark,
}: SendAnchorArgs) {
  const request = useTimelineStore((state) => state.sendAnchor);
  const consumedSeq = useRef(request.seq);
  const pendingSend = useRef<number | null | undefined>(undefined);
  const [pendingJump, setPendingJump] = useState<{ target: ReadingTarget; automatic: boolean; threadKey: string | undefined } | null>(null);
  const latestHuman = items.findLast(isHumanMessage);
  const latestRank = latestHuman === undefined ? 0 :
    compactBuffer?.rows.find((row) => row.item === latestHuman)?.rank ?? parseItemIdParts(latestHuman.item_id)?.rank ?? 0;
  const latestId = latestHuman?.item_id;
  const latestTarget = useMemo(() => latestId === undefined ? null : {
    id: latestId, rank: latestRank,
  }, [latestId, latestRank]);
  const reserveTarget = useRef<ReadingTarget | null>(null);
  const parkedTarget = useRef<ReadingTarget | null>(null);

  const writeReserve = useCallback((viewport: HTMLElement, node: HTMLElement | null) => {
    const reserve = reserveRef.current;
    if (!reserve) return;
    const height = node ? parkReserve(
      parkScrollTop(viewport.scrollTop + offsetInViewport(viewport, node), topOffset),
      viewport.clientHeight,
      viewport.scrollTop + offsetInViewport(viewport, reserve),
    ) : 0;
    const next = `${Math.ceil(height)}px`;
    if (reserve.style.height !== next) reserve.style.height = next;
  }, [reserveRef, topOffset]);

  const maintain = useCallback((viewport: HTMLElement) => {
    const row = latestTarget && rowFor(viewport, latestTarget);
    if (row || latestTarget !== reserveTarget.current) writeReserve(viewport, row);
    reserveTarget.current = latestTarget;
    if (!controller.isAnchored()) parkedTarget.current = null;
    if (row && parkedTarget.current === latestTarget) {
      // meiva:note
      // The pending strip can shrink or grow the viewport in several commits
      // around the send. Keep the alignment until the reader navigates.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
      const target = parkScrollTop(viewport.scrollTop + offsetInViewport(viewport, row), topOffset);
      if (Math.abs(viewport.scrollTop - target) > 1) {
        viewport.scrollTop = target;
        controller.notifyAnchored(snapshotOf(viewport));
      }
    }
  }, [controller, latestTarget, topOffset, writeReserve]);

  const park = useCallback((viewport: HTMLElement, row: HTMLElement) => {
    cancelBookmark();
    materializeBeforeAnchor(viewport, row);
    writeReserve(viewport, row);
    viewport.scrollTop = parkScrollTop(viewport.scrollTop + offsetInViewport(viewport, row), topOffset);
    controller.notifyAnchored(snapshotOf(viewport));
    parkedTarget.current = latestTarget && rowFor(viewport, latestTarget) === row ? latestTarget : null;
    measureAtBottom();
  }, [cancelBookmark, controller, latestTarget, measureAtBottom, topOffset, writeReserve]);

  const jumpToLatestHuman = useCallback((viewport: HTMLElement, latestRowBelowWindow: boolean) => {
    if (!latestTarget) return false;
    const row = rowFor(viewport, latestTarget);
    if (row) {
      if (offsetInViewport(viewport, row) <= topOffset + 1) return false;
      park(viewport, row);
    } else {
      if (!latestRowBelowWindow) return false;
      cancelBookmark();
      setPendingJump({ target: latestTarget, automatic: false, threadKey });
    }
    return true;
  }, [cancelBookmark, latestTarget, park, threadKey, topOffset]);

  const cancelPendingJump = useCallback(() => {
    pendingSend.current = undefined;
    parkedTarget.current = null;
    setPendingJump(null);
  }, []);

  useLayoutEffect(() => {
    pendingSend.current = undefined;
    reserveTarget.current = null;
    parkedTarget.current = null;
  }, [threadKey]);

  // meiva:note
  // A virtual row must mount before the alignment is measured. The layout
  // pass queues that window change before paint and consumes it once mounted.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
  useLayoutEffect(() => {
    const viewport = viewportRef.current
      ?? wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
    if (!viewport) return;

    if (request.seq !== consumedSeq.current) {
      consumedSeq.current = request.seq;
      pendingSend.current = isFollowing() ? request.inboundId : undefined;
    }

    if (pendingSend.current !== undefined) {
      const inboundId = pendingSend.current;
      if (!isFollowing()) pendingSend.current = undefined;
      else {
        const item = inboundId === null ? latestHuman : items.find((candidate) => candidate.inbound_id === inboundId && isHumanMessage(candidate));
        if (item) {
          pendingSend.current = undefined;
          const rank = compactBuffer?.rows.find((row) => row.item === item)?.rank ?? parseItemIdParts(item.item_id)?.rank ?? 0;
          const target = { id: item.item_id, rank };
          const row = rowFor(viewport, target);
          if (row) park(viewport, row);
          else setPendingJump({ target, automatic: true, threadKey });
        }
      }
    }

    if (pendingJump && pendingJump.threadKey === threadKey) {
      const row = rowFor(viewport, pendingJump.target);
      if (pendingJump.automatic && !isFollowing()) setPendingJump(null);
      else if (row) {
        park(viewport, row);
        setPendingJump(null);
      }
    }
  }, [compactBuffer, isFollowing, items, latestHuman, park, pendingJump, request, threadKey, viewportRef, wrapperRef]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (viewport) maintain(viewport);
  });

  const pendingTarget = pendingJump?.threadKey === threadKey ? pendingJump?.target ?? null : null;
  return { latestTarget, pendingTarget, jumpToLatestHuman, cancelPendingJump, maintain };
}
