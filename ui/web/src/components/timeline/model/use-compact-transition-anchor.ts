"use client";

import { useLayoutEffect, useRef, useState } from "react";

import {
  canonicalBufferCoordinate,
  subscribeBeforeCompactDisplayChange,
  type CompactCoordinate,
  type CompactTransitionBuffer,
} from "@/lib/layout/compact-transition";
import type { StickyController } from "@/lib/layout/sticky";
import { isReattachedTimelineContext, parseItemIdParts, standingHeadNoteIds } from "@/lib/timeline/timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";

interface CompactAnchor {
  rank: number;
  msg: number;
  block: number;
  screenTop: number;
}

export type CompactPin = Pick<CompactAnchor, "rank" | "msg" | "block">;

function parseMemberTriples(
  node: HTMLElement,
  includeMembers: boolean,
): { source: string; rank: number; id: string }[] {
  if (includeMembers && node.dataset.turnMemberSources) {
    return node.dataset.turnMemberSources
      .split(" ")
      .filter(Boolean)
      .flatMap((token) => {
        const firstColon = token.indexOf(":");
        const secondColon = firstColon > 0 ? token.indexOf(":", firstColon + 1) : -1;
        if (firstColon <= 0 || secondColon <= firstColon + 1) return [];
        const source = token.slice(0, firstColon);
        const rank = Number(token.slice(firstColon + 1, secondColon));
        const id = token.slice(secondColon + 1);
        return id && !Number.isNaN(rank) ? [{ source, rank, id }] : [];
      });
  }
  const source = node.dataset.timelineSource ?? "canonical";
  const rank = Number(node.dataset.displayRank);
  const ids = [
    node.dataset.itemId,
    ...(includeMembers ? node.dataset.turnMemberIds?.split(" ") ?? [] : []),
  ];
  return ids
    .filter((id): id is string => !!id)
    .map((id) => ({ source, rank, id }));
}

function nodeCoordinates(
  node: HTMLElement,
  bufferedCoordinates: ReadonlyMap<string, CompactCoordinate>,
  includeMembers = false,
): (CompactCoordinate | null)[] {
  return parseMemberTriples(node, includeMembers).map(({ source, rank, id }) =>
    source === "buffer"
      ? bufferedCoordinates.get(`${rank}:${id}`) ?? null
      : parseItemIdParts(id),
  );
}

function matchesAnchor(
  node: HTMLElement,
  anchor: CompactAnchor,
  bufferedCoordinates: ReadonlyMap<string, CompactCoordinate>,
  includeMembers = false,
): boolean {
  if (!includeMembers && Number(node.dataset.displayRank) !== anchor.rank) return false;
  return nodeCoordinates(node, bufferedCoordinates, includeMembers).some(
    (parts) =>
      parts !== null &&
      parts.rank === anchor.rank &&
      parts.msg === anchor.msg &&
      parts.block === anchor.block,
  );
}

function materializeNode(node: HTMLElement): boolean {
  let changed = false;
  if (node.classList.contains("timeline-item") && node.style.contentVisibility !== "visible") {
    node.style.contentVisibility = "visible";
    changed = true;
  }
  for (const child of node.querySelectorAll<HTMLElement>(".timeline-item")) {
    if (child.style.contentVisibility !== "visible") {
      child.style.contentVisibility = "visible";
      changed = true;
    }
  }
  return changed;
}

// meiva:note
// Resolve auto-sized rows up to and including the newly keyed anchor before reading
// its position, keeping measured heights until reconciliation so off-screen rows do
// not collapse back to intrinsic placeholders before paint.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
function measureRenderedAnchor(viewport: HTMLElement, anchorNode: HTMLElement): number {
  materializeNode(anchorNode);
  let anchorTop = anchorNode.getBoundingClientRect().top;
  for (;;) {
    let forced = false;
    for (const node of viewport.querySelectorAll<HTMLElement>(".timeline-item")) {
      if (node === anchorNode || node.contains(anchorNode)) break;
      if (node.style.contentVisibility === "visible") continue;
      if (node.getBoundingClientRect().top >= anchorTop) break;
      node.style.contentVisibility = "visible";
      forced = true;
    }
    if (!forced) break;
    const nextTop = anchorNode.getBoundingClientRect().top;
    if (nextTop === anchorTop) break;
    anchorTop = nextTop;
  }
  void viewport.getBoundingClientRect();
  return anchorNode.getBoundingClientRect().top;
}

function materializeViewportRows(viewport: HTMLElement): boolean {
  const bounds = viewport.getBoundingClientRect();
  let forced = false;
  for (const node of viewport.querySelectorAll<HTMLElement>(".timeline-item")) {
    if (node.style.contentVisibility === "visible") continue;
    const box = node.getBoundingClientRect();
    if (box.bottom > bounds.top && box.top < bounds.bottom) {
      node.style.contentVisibility = "visible";
      forced = true;
    }
  }
  void viewport.getBoundingClientRect();
  return forced;
}

function restoreAnchorScrollTop(viewport: HTMLElement, target: HTMLElement, screenTop: number): void {
  viewport.scrollTop += measureRenderedAnchor(viewport, target) - screenTop;
  for (let pass = 0; pass < 8; pass += 1) {
    const materialized = materializeViewportRows(viewport);
    const drift = target.getBoundingClientRect().top - screenTop;
    if (Math.abs(drift) >= 0.5) {
      viewport.scrollTop += drift;
    }
    if (!materialized || Math.abs(target.getBoundingClientRect().top - screenTop) < 0.5) {
      break;
    }
  }
}

// meiva:note
// Transfer a visible reading row across a compact re-key before the next browser paint.
// The store announces the transition before publishing it to React so layout measurement
// captures pre-mutation screen coordinates even when multiple writes batch into one render.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
export function useCompactTransitionAnchor(options: {
  controller: StickyController;
  viewportRef: { current: HTMLElement | null };
  prependAnchorRef: { current: { id: string; frontId: string | null; docTop: number } | null };
  compactBuffer: CompactTransitionBuffer | null;
}): CompactPin | null {
  const { controller, viewportRef, prependAnchorRef, compactBuffer } = options;
  const anchorRef = useRef<CompactAnchor | null>(null);
  const [pin, setPin] = useState<CompactPin | null>(null);
  const compactReplaceSeq = useTimelineStore((s) => s.compactReplaceSeq);

  useLayoutEffect(() => subscribeBeforeCompactDisplayChange((change) => {
    if (anchorRef.current) return;
    prependAnchorRef.current = null;
    if (controller.isSticky()) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    const oldHeadNotes = standingHeadNoteIds(change.items);
    for (const node of viewport.querySelectorAll<HTMLElement>(
      ".timeline-item[data-item-id][data-display-rank], [data-turn-expanded='false'][data-item-id][data-display-rank]",
    )) {
      const box = node.getBoundingClientRect();
      if (box.bottom < rect.top || box.top > rect.bottom) continue;
      const includeMembers = node.dataset.turnExpanded === "false";
      for (const candidate of parseMemberTriples(node, includeMembers)) {
        const parts = parseItemIdParts(candidate.id);
        if (!parts) continue;
        const bufferedRow = candidate.source === "buffer"
          ? change.buffer?.rows.find((row) => row.rank === candidate.rank && row.item.item_id === candidate.id)
          : null;
        const real = candidate.source === "buffer"
          ? bufferedRow?.needsCanonicalRow
          : change.items.some((item) => item.item_id === candidate.id && !item.partial &&
              !isReattachedTimelineContext(item) && !oldHeadNotes.has(candidate.id));
        if (!real) continue;
        const canonicalCoordinate = bufferedRow ? canonicalBufferCoordinate(bufferedRow) : null;
        anchorRef.current = {
          rank: candidate.rank + change.rankShift,
          msg: canonicalCoordinate?.msg ?? parts.msg - (candidate.rank === 0 && change.rankShift > 0 ? 1 : 0),
          block: parts.block,
          screenTop: box.top,
        };
        setPin({ rank: anchorRef.current.rank, msg: anchorRef.current.msg, block: anchorRef.current.block });
        return;
      }
    }
  }), [controller, viewportRef, prependAnchorRef]);

  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    if (!anchor) return;
    anchorRef.current = null;
    const viewport = viewportRef.current;
    if (!viewport) return;
    const bufferedCoordinates = new Map<string, CompactCoordinate>();
    for (const row of compactBuffer?.rows ?? []) {
      const coordinate = canonicalBufferCoordinate(row);
      if (!coordinate) continue;
      bufferedCoordinates.set(`${row.rank}:${row.item.item_id}`, coordinate);
    }
    const nodes = [...viewport.querySelectorAll<HTMLElement>(
      ".timeline-item[data-item-id][data-display-rank], [data-turn-expanded='false'][data-item-id][data-display-rank]",
    )];
    let target = nodes.find((node) => matchesAnchor(node, anchor, bufferedCoordinates)) ??
      nodes.find((node) => matchesAnchor(node, anchor, bufferedCoordinates, true));
    if (!target) {
      // meiva:note
      // A finite retention budget may exclude the anchor; fall back to the nearest
      // surviving coordinate in the same or adjacent segment.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
      let nearestDistance: [number, number, number] | null = null;
      for (const node of nodes) {
        const parts = nodeCoordinates(node, bufferedCoordinates).find((value) => value !== null);
        if (!parts) continue;
        const distance: [number, number, number] = [
          Math.abs(Number(node.dataset.displayRank) - anchor.rank),
          Math.abs(parts.msg - anchor.msg),
          Math.abs(parts.block - anchor.block),
        ];
        if (
          nearestDistance === null ||
          distance[0] < nearestDistance[0] ||
          (distance[0] === nearestDistance[0] && distance[1] < nearestDistance[1]) ||
          (distance[0] === nearestDistance[0] && distance[1] === nearestDistance[1] &&
            distance[2] < nearestDistance[2])
        ) {
          target = node;
          nearestDistance = distance;
        }
      }
    }
    if (target) {
      restoreAnchorScrollTop(viewport, target, anchor.screenTop);
    }
    setPin(null);
  }, [compactBuffer, compactReplaceSeq, viewportRef]);
  return pin;
}
