import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { Pos } from "@/lib/fleet/use-force-layout";
import type { SvgViewport } from "@/lib/fleet/use-svg-zoom-pan";
import type { ForceParams } from "./force-controls";

export interface ForceGraphNode {
  readonly id: number;
  readonly label: string | null;
  readonly status: string;
  readonly score: number;
  readonly pulse?: boolean;
  readonly ghost?: boolean;
  readonly nodeTitle?: string | null;
}

export type HoverCard = (node: ForceGraphNode) => ReactNode | null;

export interface ForceGraphEdge {
  readonly from: number;
  readonly to: number;
  readonly kind: "lineage" | "message";
  readonly weight: number;
}

export interface HoveredNodeBox {
  id: number;
  x: number;
  y: number;
  flipX: number;
  h: number;
}

function buildNeighborMap(edges: readonly ForceGraphEdge[]): Map<number, number[]> {
  const map = new Map<number, number[]>();
  for (const e of edges) {
    let a = map.get(e.from);
    if (!a) {
      a = [];
      map.set(e.from, a);
    }
    a.push(e.to);
    let b = map.get(e.to);
    if (!b) {
      b = [];
      map.set(e.to, b);
    }
    b.push(e.from);
  }
  return map;
}

export function buildConnectedNodeIds(
  edges: readonly ForceGraphEdge[],
  selectedId: number | null,
): Set<number> | null {
  if (selectedId == null) return null;
  const ids = new Set<number>();
  for (const e of edges) {
    if (e.from === selectedId) ids.add(e.to);
    if (e.to === selectedId) ids.add(e.from);
  }
  return ids;
}

export function useGraphFocus({
  edges,
  positions,
  layout,
  selectedId,
  params,
  setExtent,
  focusBounds,
}: {
  edges: readonly ForceGraphEdge[];
  positions: Map<number | string, Pos>;
  layout: SvgViewport | null;
  selectedId: number | null;
  params: ForceParams;
  setExtent: (viewport: SvgViewport) => void;
  focusBounds: (
    bounds: { minX: number; minY: number; maxX: number; maxY: number },
    viewport: SvgViewport,
    pad: number,
    fitRatio: number,
  ) => void;
}) {
  const positionsRef = useRef(positions);
  useLayoutEffect(() => {
    positionsRef.current = positions;
  });
  const layoutRef = useRef<{ positions: Map<number | string, Pos>; viewport: SvgViewport }>({
    positions,
    viewport: { minX: -100, minY: -100, w: 200, h: 200 },
  });
  useLayoutEffect(() => {
    if (!layout) return;
    layoutRef.current = {
      positions,
      viewport: layout,
    };
    setExtent(layout);
  }, [layout, positions, setExtent]);

  const neighborMap = useMemo(() => buildNeighborMap(edges), [edges]);

  const focusNode = useCallback(
    (id: number) => {
      const { positions: pos } = layoutRef.current;
      const p = positionsRef.current.get(id) ?? pos.get(id);
      if (!p) return;

      const neighbors = neighborMap.get(id) ?? [];

      let minX = p.x, minY = p.y, maxX = p.x, maxY = p.y;
      for (const nid of neighbors) {
        const np = pos.get(nid);
        if (np) {
          if (np.x < minX) minX = np.x;
          if (np.y < minY) minY = np.y;
          if (np.x > maxX) maxX = np.x;
          if (np.y > maxY) maxY = np.y;
        }
      }

      focusBounds(
        { minX, minY, maxX, maxY },
        layoutRef.current.viewport,
        params.nodeSizeMax + params.zoomPadding,
        params.zoomFitRatio,
      );
    },
    [neighborMap, params.nodeSizeMax, params.zoomPadding, params.zoomFitRatio, focusBounds],
  );

  const focusedRef = useRef<number | null>(null);
  useEffect(() => {
    if (selectedId == null) {
      focusedRef.current = null;
      return;
    }
    if (focusedRef.current === selectedId) return;
    if (!positions.has(selectedId)) return;
    focusedRef.current = selectedId;
    focusNode(selectedId);
  }, [selectedId, positions, focusNode]);

  return focusNode;
}

export function useHoverCardState(
  nodeById: ReadonlyMap<number, ForceGraphNode>,
  hoverCard: HoverCard | undefined,
) {
  const [hovered, setHovered] = useState<HoveredNodeBox | null>(null);
  const [cardScrollable, setCardScrollable] = useState(false);
  const [prevHovered, setPrevHovered] = useState<HoveredNodeBox | null>(null);
  if (prevHovered !== hovered) {
    setPrevHovered(hovered);
    if (!hovered) setCardScrollable(false);
  }
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const hoveredCard = useMemo(() => {
    if (!hovered || !hoverCard) return null;
    const node = nodeById.get(hovered.id);
    return node ? hoverCard(node) : null;
  }, [hovered, hoverCard, nodeById]);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const hoverCardRef = useRef<HTMLDivElement | null>(null);
  const legendRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => () => {
    if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
  }, []);

  useLayoutEffect(() => {
    if (!hovered) {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
      return;
    }

    // meiva:note
    // The card's content arrives after the hover (the full task is fetched on
    // demand), so its box grows from a placeholder to the real height. A
    // one-shot placement measured the placeholder and left the grown card
    // hanging off the canvas (user report 2026-10-20); the observer below
    // re-places it on every size change. Measuring the natural height while a
    // cap is applied keeps that loop stable: a cap we wrote never reads back
    // as a smaller card.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    const place = () => {
      const card = hoverCardRef.current;
      const container = containerRef.current;
      if (!card || !container) return;
      const rect = container.getBoundingClientRect();
      const legendBox = legendRef.current?.getBoundingClientRect();
      const placement = placeHoverCard({
        canvas: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
        node: { x: hovered.x, y: hovered.y, h: hovered.h, flipX: hovered.flipX },
        card: {
          width: card.offsetWidth,
          // meiva:note
          // Measure the natural height while a cap is applied: a cap we wrote must never read back
          // as a smaller card, or the re-place loop the observer drives would shrink without end.
          // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
          height: card.style.maxHeight ? card.scrollHeight : card.offsetHeight,
        },
        legend:
          legendBox === undefined
            ? null
            : {
                left: legendBox.left,
                top: legendBox.top,
                width: legendBox.width,
                height: legendBox.height,
              },
      });
      card.style.left = `${placement.left}px`;
      card.style.top = `${placement.top}px`;
      if (card.style.maxHeight !== placement.maxHeight) card.style.maxHeight = placement.maxHeight;
      const scrollable = placement.maxHeight !== "";
      card.style.overflowY = scrollable ? "auto" : "";
      setCardScrollable((current) => (current === scrollable ? current : scrollable));
    };

    place();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => place());
    if (hoverCardRef.current) observer.observe(hoverCardRef.current);
    return () => observer.disconnect();
  }, [hovered]);

  return {
    hovered,
    setHovered,
    cardScrollable,
    hideTimerRef,
    hoveredCard,
    containerRef,
    hoverCardRef,
    legendRef,
  };
}

// meiva:note
// Where the hover card goes, decided from the boxes alone: the canvas and node rectangles the page
// reports, the card's measured size and the legend's rectangle. Nothing here reads the DOM, so the
// placement rules (beside the node, flip, centre on a narrow canvas, lift over the legend, cap the
// height when neither side holds the card) are decided by values a test can state.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
export interface HoverCardBox {
  readonly width: number;
  readonly height: number;
}

export interface HoverCardNodeBox {
  readonly x: number;
  readonly y: number;
  readonly h: number;
  readonly flipX: number;
}

export interface HoverCardRect {
  readonly left: number;
  readonly top: number;
  readonly width: number;
  readonly height: number;
}

export interface HoverCardPlacement {
  readonly left: number;
  readonly top: number;
  readonly maxHeight: string;
}

export const HOVER_CARD_GAP = 14;
export const HOVER_CARD_PAD = 4;

export function placeHoverCard({
  canvas,
  node,
  card,
  legend,
  gap = HOVER_CARD_GAP,
  pad = HOVER_CARD_PAD,
}: {
  readonly canvas: HoverCardRect;
  readonly node: HoverCardNodeBox;
  readonly card: HoverCardBox;
  readonly legend: HoverCardRect | null;
  readonly gap?: number;
  readonly pad?: number;
}): HoverCardPlacement {
  const maxX = canvas.width - pad;
  const maxY = canvas.height - pad;
  const x = node.x - canvas.left;
  const y = node.y - canvas.top;
  const nodeLeft = node.flipX - canvas.left;
  const nodeBottom = y + node.h;
  const cardW = card.width;
  const cardH = card.height;

  const fitsRight = x + gap + cardW <= maxX;
  const fitsLeft = nodeLeft - gap - cardW >= pad;
  const beside = fitsRight || fitsLeft;
  const left = beside
    ? fitsRight
      ? x + gap
      : nodeLeft - gap - cardW
    : Math.max(pad, Math.min((nodeLeft + x) / 2 - cardW / 2, maxX - cardW));

  let bottomLimit = maxY;
  if (legend !== null) {
    const legendLeft = legend.left - canvas.left;
    const legendRight = legend.left + legend.width - canvas.left;
    if (left < legendRight && left + cardW > legendLeft) {
      bottomLimit = Math.min(bottomLimit, legend.top - canvas.top - gap);
    }
  }

  const belowTop = beside ? y + gap : nodeBottom + gap;
  const aboveTop = y - gap - cardH;
  const fitsBelow = belowTop + cardH <= bottomLimit;
  const fitsAbove = aboveTop >= pad;
  const preferAbove = !beside;
  let top: number;
  let cap = "";
  if (preferAbove ? fitsAbove : fitsBelow) {
    top = preferAbove ? aboveTop : belowTop;
  } else if (preferAbove ? fitsBelow : fitsAbove) {
    top = preferAbove ? belowTop : aboveTop;
  } else {
    const belowSpace = bottomLimit - belowTop;
    const aboveSpace = y - gap - pad;
    if (belowSpace >= aboveSpace) {
      top = belowTop;
      cap = `${Math.max(0, belowSpace)}px`;
    } else {
      top = pad;
      cap = `${Math.max(0, aboveSpace)}px`;
    }
  }

  return { left: Math.max(pad, left), top: Math.max(pad, top), maxHeight: cap };
}

// meiva:note
// How long a card the pointer can reach stays up after the pointer leaves the node: the trip across
// the gap between node and card takes a fraction of this, and anything longer would feel stuck.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
export const HOVER_CARD_HIDE_GRACE_MS = 200;

export type HoverCardLeave = "hide" | "hold" | "wait";

// meiva:note
// What a pointer leaving the node does next. A card the pointer can reach (height-capped, so its
// body scrolls and it takes pointer events) must survive the trip across the gap between the node
// and the card: it waits out the grace window, unless the pointer already landed on it. A card the
// pointer cannot touch has nothing to reach for and goes at once.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
export function hoverCardLeave({
  scrollable,
  pointerOnCard,
}: {
  readonly scrollable: boolean;
  readonly pointerOnCard: boolean;
}): HoverCardLeave {
  if (!scrollable) return "hide";
  return pointerOnCard ? "hold" : "wait";
}
