import { useCallback, useLayoutEffect, useRef, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { isReattachedTimelineContext, standingHeadNoteIds } from "@/lib/timeline/timeline";

import { materializeBeforeAnchor } from "../model/materialize-prefix";

export interface PrependAnchor {
  id: string;
  rank: number;
  frontId: string | null;
  docTop: number;
}

export function usePrependAnchor({
  viewportRef,
  wrapperRef,
  itemsRef,
  items,
  loadingOlder,
  scrollByDelta,
}: {
  viewportRef: RefObject<HTMLElement | null>;
  wrapperRef: RefObject<HTMLDivElement | null>;
  itemsRef: RefObject<BackendTimelineItem[]>;
  items: BackendTimelineItem[];
  loadingOlder: boolean;
  scrollByDelta: (viewport: HTMLElement, delta: number) => void;
}) {
  const pendingAnchorRef = useRef<PrependAnchor | null>(null);
  const prevLoadingOlderRef = useRef(loadingOlder);

  const captureAnchor = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const currentItems = itemsRef.current;
    const headNoteIds = standingHeadNoteIds(currentItems);
    const contextIds = new Set(
      currentItems
        .filter((it) => isReattachedTimelineContext(it) || headNoteIds.has(it.item_id))
        .map((it) => it.item_id),
    );
    const vpRect = viewport.getBoundingClientRect();
    let anchorNode: HTMLElement | null = null;
    let contextNode: HTMLElement | null = null;
    for (const n of viewport.querySelectorAll<HTMLElement>(
      ".timeline-item, [data-turn-expanded='false'][data-item-id]",
    )) {
      const id = n.dataset.itemId;
      if (!id || id.startsWith("_")) continue;
      if (contextIds.has(id)) {
        contextNode ??= n;
        continue;
      }
      const r = n.getBoundingClientRect();
      if (r.bottom >= vpRect.top && r.top <= vpRect.bottom) {
        anchorNode = n;
        break;
      }
    }
    anchorNode ??= contextNode;
    const anchorId = anchorNode?.dataset.itemId;
    if (anchorNode && anchorId) {
      const frontRealId =
        currentItems.find(
          (it) =>
            !isReattachedTimelineContext(it) &&
            !headNoteIds.has(it.item_id) &&
            !it.item_id.startsWith("_"),
        )?.item_id ?? null;
      pendingAnchorRef.current = {
        id: anchorId,
        rank: Number(anchorNode.dataset.displayRank ?? 0),
        frontId: frontRealId,
        docTop: anchorNode.getBoundingClientRect().top + viewport.scrollTop,
      };
    }
  }, [itemsRef, viewportRef]);

  useLayoutEffect(() => {
    if (loadingOlder && !prevLoadingOlderRef.current && pendingAnchorRef.current === null) {
      captureAnchor();
    }
    prevLoadingOlderRef.current = loadingOlder;
  }, [loadingOlder, captureAnchor]);

  useLayoutEffect(() => {
    const anchor = pendingAnchorRef.current;
    if (!anchor) return;
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;
    const escapedId = CSS.escape(anchor.id);
    const escapedRankId = CSS.escape(`${anchor.rank}:${anchor.id}`);
    const findNode = () =>
      viewport.querySelector<HTMLElement>(`.timeline-item[data-item-id="${escapedId}"][data-display-rank="${anchor.rank}"]`) ??
      viewport.querySelector<HTMLElement>(`[data-turn-expanded="false"][data-item-id="${escapedId}"][data-display-rank="${anchor.rank}"]`) ??
      viewport.querySelector<HTMLElement>(`[data-turn-member-ranks~="${escapedRankId}"], [data-turn-member-ids~="${escapedId}"][data-display-rank="${anchor.rank}"]`);

    const headNoteIds = standingHeadNoteIds(items);
    const frontRealId = items.find(
      (it) =>
        !isReattachedTimelineContext(it) &&
        !headNoteIds.has(it.item_id) &&
        !it.item_id.startsWith("_"),
    )?.item_id ?? null;
    if (frontRealId === anchor.frontId) {
      const node = findNode();
      if (!node) {
        pendingAnchorRef.current = null;
        return;
      }
      anchor.docTop = node.getBoundingClientRect().top + viewport.scrollTop;
      return;
    }

    const anchorNode = findNode();
    if (!anchorNode) {
      pendingAnchorRef.current = null;
      return;
    }
    const cvSaved = materializeBeforeAnchor(viewport, anchorNode);
    void viewport.getBoundingClientRect();
    const node = findNode();
    if (!node) {
      for (const { el, cv } of cvSaved) el.style.contentVisibility = cv;
      pendingAnchorRef.current = null;
      return;
    }
    const delta = node.getBoundingClientRect().top + viewport.scrollTop - anchor.docTop;

    if (delta <= 0) {
      for (const { el, cv } of cvSaved) el.style.contentVisibility = cv;
      pendingAnchorRef.current = null;
      return;
    }
    scrollByDelta(viewport, delta);
    pendingAnchorRef.current = null;
  }, [items, scrollByDelta, viewportRef, wrapperRef]);

  return { pendingAnchorRef, captureAnchor };
}
