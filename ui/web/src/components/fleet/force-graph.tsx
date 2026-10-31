// Force Graph — the shared force-directed canvas behind the fleet Graph View
// (agent nodes) and the Task Graph (task nodes).
//
// One rendering / interaction / parameter system for both graphs so they can
// never drift: the same d3-force physics (useForceLayout), the same
// zoom/pan/reset/focus interactions, the same edge styling, selection ring,
// hover treatment, ForceControls gear, stats bar and zoom reset. The ONLY
// visual difference between the two views is node shape — "circle" for agents,
// "square" for tasks — everything else is this one component.
//
// Display data (status color, score, ghost flag) is always read from the latest
// props on render — the simulation only owns positions, so live SSE updates
// recolor/resize nodes in place without disturbing the layout.

"use client";

import { useTranslations } from "next-intl";
import { memo, useMemo, type ReactNode } from "react";

import {
  useForceLayout,
  type ForceLayoutOptions,
  type SimLink,
  type SimNode,
} from "@/lib/fleet/use-force-layout";
import { useSvgZoomPan } from "@/lib/fleet/use-svg-zoom-pan";
import { cn } from "@/lib/format/utils";

import { ForceControls, FORCE_GROUPS, type ForceGroup, type ForceParams } from "./force-controls";
import {
  buildConnectedNodeIds,
  useGraphFocus,
  useHoverCardState,
  type ForceGraphEdge,
  type ForceGraphNode,
  type HoverCard,
} from "./force-graph-geometry";
import { LABEL_LINE_HEIGHT, LABEL_MIN_ZOOM, radiusOf, wrapLabel } from "./presentation/labels";
import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout/layout";

// meiva:note
// A graph this size settles in manual, time-budgeted slices instead of one React
// render per physics tick. Every tick used to commit a fresh position map, and
// every commit repaints the whole SVG: the preview's 1,949-node graph spent
// 4.4 s of main-thread time in the first six seconds at 4x CPU throttle, nearly
// all of it style/layout rather than script. Below the threshold the live
// settle stays - a small graph animating into place is the point of it.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
const PREWARM_NODE_THRESHOLD = 200;
const PREWARM_TICKS = 240;

export function layoutOptions(nodeCount: number): ForceLayoutOptions {
  return nodeCount >= PREWARM_NODE_THRESHOLD ? { prewarmTicks: PREWARM_TICKS } : {};
}

export const ForceGraph = memo(function ForceGraph({
  nodes,
  edges,
  shape,
  statusText,
  selectedId,
  onSelect,
  onOpen,
  params,
  setParams,
  resetParams,
  groups = FORCE_GROUPS,
  statsText,
  legend,
  overlayLeft,
  hoverCard,
  ariaLabel,
}: {
  nodes: readonly ForceGraphNode[];
  edges: readonly ForceGraphEdge[];
  shape: "circle" | "square";
  /** Status key → text-* color class (the node paints with fill="currentColor"). */
  statusText: Record<string, string>;
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  /** Double-click on a node (agents: their timeline; tasks: the owner's). */
  onOpen: (id: number) => void;
  params: ForceParams;
  setParams: (p: ForceParams) => void;
  resetParams: () => void;
  groups?: ForceGroup[];
  /** Stats bar text (bottom-left); omitted → no bar. */
  statsText?: string | null;
  /** View-specific status / size legend (bottom-right); omitted → no panel. */
  legend?: ReactNode;
  /** Extra control rendered beside the layout gear (e.g. the window selector). */
  overlayLeft?: ReactNode;
  /** Instant hover detail card; see HoverCard. Absent → native <title> tooltip. */
  hoverCard?: HoverCard;
  ariaLabel?: string;
}) {
  const t = useTranslations("fleet.forceGraph");
  const graphAriaLabel = ariaLabel ?? t("defaultAriaLabel");
  // Max score across the graph — the radius normalizer. Must match the value
  // used for the sim collide radii so the rendered node matches its collision
  // circle. floor 1 avoids div-by-zero when every score is 0.
  const maxScore = useMemo(() => Math.max(...nodes.map((n) => n.score), 1), [nodes]);
  // Edge opacity spans this graph's actual weight range, so the weakest and
  // strongest relationships remain visually distinct as the graph changes.
  const maxEdgeWeight = useMemo(() => Math.max(...edges.map((e) => e.weight), 1), [edges]);
  const nodeById = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  // Simulation nodes: collide radius = the rendered radius for circles, the
  // half-diagonal (r·√2) for squares — a square claims its full footprint.
  // Recomputed on params change so the Node sliders resize the collision
  // circles live (useForceLayout tracks radii in a ref).
  const simNodes = useMemo<SimNode[]>(
    () =>
      nodes.map((n) => ({
        id: n.id,
        r: radiusOf(n.score, maxScore, params.nodeSizeMin, params.nodeSizeMax) * (shape === "square" ? Math.SQRT2 : 1),
      })),
    [nodes, maxScore, params.nodeSizeMin, params.nodeSizeMax, shape],
  );
  const simLinks = useMemo<SimLink[]>(
    () => edges.map((e) => ({ source: e.from, target: e.to })),
    [edges],
  );
  const { positions, layout } = useForceLayout(simNodes, simLinks, params, layoutOptions(simNodes.length));

  const {
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
  } = useHoverCardState(nodeById, hoverCard);

  // User zoom/pan as an SVG transform on the content <g> (translate + scale),
  // separate from the fit-to-content viewBox base frame. d3-zoom owns wheel /
  // pinch / drag; programmatic transforms (reset, focus) flow through the same
  // "zoom" handler. identity transform == fit-to-content.
  const { attachZoom, setExtent, transform, animateZoom, focusBounds, resetZoom, endZoomTransition } = useSvgZoomPan([
    [-120, -120],
    [120, 120],
  ]);

  const focusNode = useGraphFocus({
    edges,
    positions,
    layout,
    selectedId,
    params,
    setExtent,
    focusBounds,
  });

  // Nodes directly connected to the selection (via any edge) — these stay lit
  // while the rest dims.
  const connectedNodeIds = useMemo(() => buildConnectedNodeIds(edges, selectedId), [edges, selectedId]);

  // The views show their own empty / loading placeholders; the shell (stats
  // bar + controls) still renders so an empty canvas keeps its chrome.
  const { placed, minX, minY, w, h } = layout ?? {
    placed: [],
    minX: 0,
    minY: 0,
    w: 0,
    h: 0,
  };
  const viewSpan = Math.max(w, h);
  const viewScale = viewSpan > 900 ? Math.min(Math.sqrt(viewSpan / 900), 2) : 1;

  return (
    <div ref={containerRef} className={cn("relative h-full w-full", OVERFLOW_HIDDEN)}>
      {layout ? (
      <svg
        ref={attachZoom}
        className="h-full w-full select-none touch-none"
        viewBox={`${minX} ${minY} ${w} ${h}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={graphAriaLabel}
        onClick={(ev) => {
          if (ev.target === ev.currentTarget) onSelect(null);
        }}
      >
        <g
          transform={transform.toString()}
          style={{ transition: animateZoom ? "transform 0.4s ease" : "none" }}
          onTransitionEnd={(ev) => {
            endZoomTransition(ev.propertyName);
          }}
        >
          <g>
            {edges.map((e, i) => {
              const a = positions.get(e.from);
              const b = positions.get(e.to);
              if (!a || !b) return null;
              const isMessage = e.kind === "message";
              const isIncident =
                connectedNodeIds != null &&
                (e.from === selectedId || e.to === selectedId);
              const isDimmed = connectedNodeIds != null && !isIncident;
              const normalizedWeight = Math.min(Math.max(e.weight / maxEdgeWeight, 0), 1);
              const opacity = isIncident
                ? 0.96
                : isDimmed
                  ? isMessage
                    ? 0.2
                    : 0.48
                  : isMessage
                    ? 0.24 + 0.61 * normalizedWeight
                    : 0.86;
              const weightSw = Math.sqrt(e.weight) * 1.2;
              const baseSw = isIncident
                ? (isMessage ? 2.2 : 3.6) + weightSw
                : (isMessage ? 0.8 : 2.1) + weightSw;
              const sw = baseSw * viewScale;
              return (
                <line
                  key={`${e.from}-${e.to}-${e.kind}-${i}`}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  className="text-muted-foreground"
                  stroke="currentColor"
                  strokeOpacity={opacity}
                  strokeWidth={sw}
                />
              );
            })}
          </g>

          <g>
            {placed.map(({ node: sim, p }) => {
              // meiva:note
              // The layout's id is the union d3 accepts; only a numeric one can name a node here,
              // and a non-numeric id simply draws nothing.
              // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
              const n = typeof sim.id === "number" ? nodeById.get(sim.id) : undefined;
              if (!n) return null;
              const r = radiusOf(n.score, maxScore, params.nodeSizeMin, params.nodeSizeMax);
              const isHovered = hovered?.id === n.id;
              const isRinged = selectedId === n.id;
              const isGhost = n.ghost === true;
              const fill = statusText[n.status] ?? "text-slate-400";
              const showLabel = transform.k >= LABEL_MIN_ZOOM;
              const labelLines = showLabel && n.label ? wrapLabel(n.label, r) : [];
              const totalTextLines = 1 + labelLines.length;
              const firstTextLineY = -((totalTextLines - 1) * LABEL_LINE_HEIGHT) / 2;
              return (
                <g
                  key={n.id}
                  data-node-id={n.id}
                  transform={`translate(${p.x},${p.y})`}
                  className={cn("cursor-pointer", isGhost && !isHovered && "opacity-40")}
                  onPointerDown={(ev) => {
                    ev.stopPropagation();
                  }}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    if (selectedId === n.id) return;
                    onSelect(n.id);
                    focusNode(n.id);
                  }}
                  onDoubleClick={(ev) => {
                    ev.stopPropagation();
                    onOpen(n.id);
                  }}
                  onMouseEnter={(ev) => {
                    clearHideTimer();
                    const shapeEl = ev.currentTarget.querySelector("[data-node-shape]") ?? ev.currentTarget;
                    const box = shapeEl.getBoundingClientRect();
                    setHovered((cur) =>
                      cur !== null &&
                      cur.id === n.id &&
                      cur.x === box.right &&
                      cur.y === box.top &&
                      cur.flipX === box.left &&
                      cur.h === box.height
                        ? cur
                        : { id: n.id, x: box.right, y: box.top, flipX: box.left, h: box.height },
                    );
                  }}
                  onMouseLeave={(ev) => {
                    const card = hoverCardRef.current;
                    const pointerOnPartner =
                      card !== null && ev.relatedTarget instanceof Node && card.contains(ev.relatedTarget);
                    leaveHoverTarget(n.id, pointerOnPartner);
                  }}
                >
                  {hoverCard == null ? (
                    <title>{n.nodeTitle ?? (n.label ? `${n.label} (#${n.id})` : `#${n.id}`)}</title>
                  ) : null}
                  {isRinged ? (
                    shape === "circle" ? (
                      <circle
                        r={r + 6}
                        className="text-sky-400"
                        fill="none"
                        stroke="currentColor"
                        strokeOpacity={0.7}
                        strokeWidth={2}
                        strokeDasharray="4 3"
                        pointerEvents="none"
                      />
                    ) : (
                      <rect
                        x={-(r + 6)}
                        y={-(r + 6)}
                        width={2 * (r + 6)}
                        height={2 * (r + 6)}
                        rx={r * 0.25}
                        className="text-sky-400"
                        fill="none"
                        stroke="currentColor"
                        strokeOpacity={0.7}
                        strokeWidth={2}
                        strokeDasharray="4 3"
                        pointerEvents="none"
                      />
                    )
                  ) : null}
                  {shape === "circle" ? (
                    <circle
                      data-node-shape=""
                      r={r}
                      className={cn(fill, n.pulse && "animate-pulse")}
                      fill="currentColor"
                      stroke="var(--background)"
                      strokeWidth={1.5}
                      strokeDasharray={isGhost ? "3 2" : undefined}
                      opacity={isHovered ? 1 : 0.92}
                    />
                  ) : (
                    <rect
                      data-node-shape=""
                      x={-r}
                      y={-r}
                      width={2 * r}
                      height={2 * r}
                      rx={r * 0.25}
                      className={cn(fill, n.pulse && "animate-pulse")}
                      fill="currentColor"
                      stroke="var(--background)"
                      strokeWidth={1.5}
                      strokeDasharray={isGhost ? "3 2" : undefined}
                      opacity={isHovered ? 1 : 0.92}
                    />
                  )}
                  {showLabel ? (
                    <text
                      textAnchor="middle"
                      dominantBaseline="central"
                      className="fill-white text-[6px] font-mono font-semibold"
                      stroke="rgba(0,0,0,0.35)"
                      strokeWidth={0.75}
                      paintOrder="stroke"
                      style={{ pointerEvents: "none" }}
                    >
                      <tspan x={0} y={firstTextLineY} dominantBaseline="central">
                        #{n.id}
                      </tspan>
                      {labelLines.map((line, index) => (
                        <tspan key={index} x={0} dy={LABEL_LINE_HEIGHT} dominantBaseline="central">
                          {line}
                        </tspan>
                      ))}
                    </text>
                  ) : null}
                </g>
              );
            })}
          </g>
        </g>
      </svg>
      ) : nodes.length > 0 ? (
        <div className={cn("h-full items-center justify-center text-xs text-muted-foreground", FLEX)}>
          {t("loading")}
        </div>
      ) : null}

      {/* Stats bar: node / edge counts. */}
      {statsText != null ? (
        <div className="pointer-events-none absolute bottom-3 left-3 rounded-md border border-border bg-background/80 px-3 py-1.5 text-[10px] text-muted-foreground tabular-nums backdrop-blur">
          {statsText}
        </div>
      ) : null}

      {legend != null ? (
        <div ref={legendRef} className="pointer-events-none absolute bottom-3 right-3 rounded-md border border-border bg-background/80 px-3 py-1.5 text-[10px] text-muted-foreground backdrop-blur">
          {legend}
        </div>
      ) : null}

      {hoveredCard != null && hovered != null ? (
        <div
          ref={hoverCardRef}
          role="tooltip"
          className="pointer-events-auto absolute z-20"
          style={{ left: 0, top: 0 }}
          onMouseEnter={() => {
            clearHideTimer();
          }}
          onMouseLeave={(ev) => {
            const anchorNode = containerRef.current?.querySelector(`[data-node-id="${hovered.id}"]`) ?? null;
            const pointerOnPartner =
              anchorNode !== null && ev.relatedTarget instanceof Node && anchorNode.contains(ev.relatedTarget);
            leaveHoverTarget(hovered.id, pointerOnPartner);
          }}
        >
          <div ref={hoverBridgeRef} data-testid="hover-card-bridge" aria-hidden className="absolute" />
          <div key={hovered.id} ref={hoverScrollRef} data-hover-card-scroll className="overscroll-contain rounded-lg">
            <div ref={hoverContentRef}>{hoveredCard}</div>
          </div>
        </div>
      ) : null}

      {/* Toolbar — inset from the canvas edge; wheel/pinch zoom and drag pan
          live on the SVG itself, the reset button returns to the
          fit-to-content frame. Order (user ruling 2026-08-06): settings,
          reset zoom, then view extras (the time-window selector). */}
      <div className={cn("pointer-events-auto absolute left-3 top-3 items-center gap-1", FLEX)}>
        <ForceControls params={params} setParams={setParams} reset={resetParams} groups={groups} />
        <button
          type="button"
          className={cn("size-6 items-center justify-center rounded border border-border bg-background/80 text-[10px] text-muted-foreground backdrop-blur hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label={t("resetZoom")}
          onClick={resetZoom}
          disabled={transform.k === 1 && transform.x === 0 && transform.y === 0}
        >
          ↺
        </button>
        {overlayLeft}
      </div>
    </div>
  );
});
