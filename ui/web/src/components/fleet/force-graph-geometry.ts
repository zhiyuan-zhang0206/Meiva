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
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearHideTimer = useCallback(() => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
  }, []);

  const leaveHoverTarget = useCallback(
    (nodeId: number, pointerOnPartner: boolean) => {
      if (hoverCardLeave({ pointerOnPartner }) === "hold") return;
      clearHideTimer();
      hideTimerRef.current = setTimeout(
        () => setHovered((cur) => (cur?.id === nodeId ? null : cur)),
        HOVER_CARD_HIDE_GRACE_MS,
      );
    },
    [clearHideTimer],
  );

  const hoveredCard = useMemo(() => {
    if (!hovered || !hoverCard) return null;
    const node = nodeById.get(hovered.id);
    return node ? hoverCard(node) : null;
  }, [hovered, hoverCard, nodeById]);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const hoverCardRef = useRef<HTMLDivElement | null>(null);
  const hoverBridgeRef = useRef<HTMLDivElement | null>(null);
  const hoverScrollRef = useRef<HTMLDivElement | null>(null);
  const hoverContentRef = useRef<HTMLDivElement | null>(null);
  const legendRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => () => clearHideTimer(), [clearHideTimer]);

  useLayoutEffect(() => {
    if (!hovered) {
      clearHideTimer();
      return;
    }

    // meiva:note
    // The card's content arrives after the hover (the full task is fetched on
    // demand), so its box grows from a placeholder to the real height. Observing
    // the uncapped inner content wrapper re-places the card on every growth while
    // keeping the outer tooltip shell overflow-visible so the safe-zone bridge
    // spanning the gap to the node is never clipped by overflow-y:auto.
    // meiva:link ui/web/src/docs/frontend-components/force-graph.meiva.okf.md
    const place = () => {
      const card = hoverCardRef.current;
      const bridge = hoverBridgeRef.current;
      const scroller = hoverScrollRef.current;
      const content = hoverContentRef.current;
      const container = containerRef.current;
      if (!card || !bridge || !scroller || !content || !container) return;
      const rect = container.getBoundingClientRect();
      const legendBox = legendRef.current?.getBoundingClientRect();
      const canvas = { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
      const node = { x: hovered.x, y: hovered.y, h: hovered.h, flipX: hovered.flipX };
      const cardBox = { width: content.offsetWidth, height: content.offsetHeight };
      const placement = placeHoverCard({
        canvas,
        node,
        card: cardBox,
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
      if (scroller.style.maxHeight !== placement.maxHeight) {
        scroller.style.maxHeight = placement.maxHeight;
      }
      scroller.style.overflowY = placement.maxHeight !== "" ? "auto" : "";
      const bridgeStyle = hoverCardBridge({ canvas, node, card: cardBox, placement });
      bridge.style.left = bridgeStyle.left;
      bridge.style.right = bridgeStyle.right;
      bridge.style.top = bridgeStyle.top;
      bridge.style.bottom = bridgeStyle.bottom;
      bridge.style.width = bridgeStyle.width;
      bridge.style.height = bridgeStyle.height;
    };

    place();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => place());
    if (hoverContentRef.current) observer.observe(hoverContentRef.current);
    if (containerRef.current) observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, [hovered, clearHideTimer]);

  return {
    hovered,
    setHovered,
    clearHideTimer,
    leaveHoverTarget,
    hoveredCard,
    containerRef,
    hoverCardRef,
    hoverBridgeRef,
    hoverScrollRef,
    hoverContentRef,
    legendRef,
  };
}

// meiva:note
// Where the hover card goes, decided from the boxes alone: the canvas and node rectangles the page
// reports, the card's measured size and the legend's rectangle. Nothing here reads the DOM, so the
// placement rules (beside the node top-aligned, flipped to the left edge, slid within the column or
// capped to the column height when tall, centred on a narrow canvas, lifted over the legend) are
// decided by values a test can state.
// meiva:link ui/web/src/docs/frontend-components/force-graph.meiva.okf.md
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

export type HoverCardSide = "right" | "left" | "above" | "below";

export interface HoverCardPlacement {
  readonly left: number;
  readonly top: number;
  readonly maxHeight: string;
  readonly side: HoverCardSide;
}

export interface HoverCardBridgeStyle {
  readonly left: string;
  readonly right: string;
  readonly top: string;
  readonly bottom: string;
  readonly width: string;
  readonly height: string;
}

export const HOVER_CARD_GAP = 6;
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

  if (beside) {
    const side: HoverCardSide = fitsRight ? "right" : "left";
    const columnSpace = Math.max(0, bottomLimit - pad);
    if (cardH <= columnSpace) {
      const top = Math.max(pad, Math.min(y, bottomLimit - cardH));
      return { left: Math.max(pad, left), top, maxHeight: "", side };
    }
    return {
      left: Math.max(pad, left),
      top: pad,
      maxHeight: `${columnSpace}px`,
      side,
    };
  }

  const belowTop = nodeBottom + gap;
  const aboveTop = y - gap - cardH;
  const fitsBelow = belowTop + cardH <= bottomLimit;
  const fitsAbove = aboveTop >= pad;
  let top: number;
  let cap = "";
  let side: HoverCardSide;
  if (fitsAbove) {
    top = aboveTop;
    side = "above";
  } else if (fitsBelow) {
    top = belowTop;
    side = "below";
  } else {
    const belowSpace = bottomLimit - belowTop;
    const aboveSpace = y - gap - pad;
    if (belowSpace >= aboveSpace) {
      top = belowTop;
      cap = `${Math.max(0, belowSpace)}px`;
      side = "below";
    } else {
      top = pad;
      cap = `${Math.max(0, aboveSpace)}px`;
      side = "above";
    }
  }

  return { left: Math.max(pad, left), top: Math.max(pad, top), maxHeight: cap, side };
}

export function hoverCardBridge({
  canvas,
  node,
  card,
  placement,
  gap = HOVER_CARD_GAP,
}: {
  readonly canvas: HoverCardRect;
  readonly node: HoverCardNodeBox;
  readonly card: HoverCardBox;
  readonly placement: HoverCardPlacement;
  readonly gap?: number;
}): HoverCardBridgeStyle {
  const y = node.y - canvas.top;
  const nodeBottom = y + node.h;
  const renderedH =
    placement.maxHeight !== "" ? Number.parseFloat(placement.maxHeight) : card.height;
  if (placement.side === "right" || placement.side === "left") {
    const topOffset = Math.min(0, y - placement.top);
    const bottomOffset = Math.min(0, placement.top + renderedH - nodeBottom);
    return {
      left: placement.side === "right" ? `${-gap}px` : "",
      right: placement.side === "left" ? `${-gap}px` : "",
      top: `${topOffset}px`,
      bottom: `${bottomOffset}px`,
      width: `${gap}px`,
      height: "",
    };
  }
  return {
    left: "0px",
    right: "0px",
    top: placement.side === "below" ? `${-gap}px` : "",
    bottom: placement.side === "above" ? `${-gap}px` : "",
    width: "",
    height: `${gap}px`,
  };
}

// meiva:note
// Symmetrical grace window for node-to-card and card-to-node pointer travel: direct transitions
// across the safe-zone bridge hold immediately, while diagonal trips wait out the grace window.
// meiva:link ui/web/src/docs/frontend-components/force-graph.meiva.okf.md
export const HOVER_CARD_HIDE_GRACE_MS = 200;

export type HoverCardLeave = "hold" | "wait";

export function hoverCardLeave({
  pointerOnPartner,
}: {
  readonly pointerOnPartner: boolean;
}): HoverCardLeave {
  return pointerOnPartner ? "hold" : "wait";
}
