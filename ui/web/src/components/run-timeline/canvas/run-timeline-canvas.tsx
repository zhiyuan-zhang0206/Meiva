"use client";

// meiva:note
// One row's canvas: it reads the viewport from the store in its next animation frame, lays its row out
// for that viewport, and repaints only when what it would draw changed (`paintKey`) — so a wheel tick
// costs one paint per row and no React render of the row tree (task #5738). A pointer layer turns a
// position into the item under it (`hitTest`).
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md

import { useEffect, useRef } from "react";

import { buildHitIndex, hitTest, type HitIndex, type RowLayout } from "../model/timeline-canvas-hit";
import { layoutsFor } from "../model/timeline-canvas-model";
import type { NavData, NavItem } from "../model/timeline-nav-rows";
import type { AxisMap } from "../model/timeline-model-axis";
import type { ViewportStore } from "../viewport/viewport-store";
import { countDraw, countPaintMs } from "../model/render-stats";
import { useColorResolver } from "./resolve-color";
import type { PaintCtx } from "./run-timeline-paint";

export function TrackCanvas({
  store,
  data,
  rows,
  row,
  axis,
  width,
  height,
  paint,
  paintKey,
  testId,
  onHit,
  onChoose,
}: {
  store: ViewportStore;
  data: NavData;
  rows: readonly string[];
  row: string;
  axis: AxisMap;
  width: number;
  height: number;
  paint: (p: PaintCtx, layout: RowLayout) => void;
  paintKey: (layout: RowLayout) => string;
  testId: string;
  onHit: (item: NavItem | null) => void;
  onChoose: (item: NavItem) => void;
}) {
  const resolve = useColorResolver();
  const canvas = useRef<HTMLCanvasElement>(null);
  const layout = useRef<RowLayout | null>(null);
  const index = useRef<HitIndex | null>(null);
  const last = useRef<NavItem["selection"] | null>(null);
  const painted = useRef<string | null>(null);
  const bump = useRef<() => void>(() => undefined);

  const latest = useRef({ paint, paintKey });

  // meiva:note
  // The frame callback reads the newest closure, so a render that changed the selection hands over a new
  // paint and key without restarting the frame loop.
  // meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
  useEffect(() => {
    latest.current = { paint, paintKey };
  });
  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d") ?? null;
    if (el === null || ctx === null) return;
    let frame: number | null = null;
    const draw = () => {
      frame = null;
      const view = store.get();
      const here = layoutsFor(data, axis, axis.viewU(view), width, rows).get(row);
      if (here === undefined) return;
      layout.current = here;
      const key = latest.current.paintKey(here);
      if (painted.current === key) return;
      const dpr = window.devicePixelRatio || 1;
      const pixelW = Math.max(1, Math.round(width * dpr));
      const pixelH = Math.max(1, Math.round(height * dpr));
      if (el.width !== pixelW) el.width = pixelW;
      if (el.height !== pixelH) el.height = pixelH;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      countDraw();
      ctx.clearRect(0, 0, width, height);
      const style = getComputedStyle(el);
      const started = performance.now();
      latest.current.paint(
        {
          ctx,
          resolve,
          width,
          height,
          dpr,
          font: style.fontFamily || "sans-serif",
          trackBg: "color-mix(in srgb, var(--muted) 40%, var(--card))",
        },
        here,
      );
      countPaintMs(performance.now() - started);
      index.current = buildHitIndex(here);
      painted.current = key;
    };
    const schedule = () => {
      if (frame !== null) return;
      frame = requestAnimationFrame(draw);
    };
    bump.current = schedule;
    schedule();
    const unsubscribe = store.subscribe(schedule);
    return () => {
      unsubscribe();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [store, data, axis, rows, row, width, height, resolve]);
  // meiva:note
  // A render that changed what the row would draw (a new selection, a new highlight) asks for a frame of its
  // own: the viewport did not move, but the picture it draws did.
  // meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
  useEffect(() => {
    bump.current();
  });

  const itemAt = (event: React.PointerEvent | React.MouseEvent): NavItem | null => {
    const here = index.current;
    if (here === null) return null;
    const key = hitTest(here, event.clientX - event.currentTarget.getBoundingClientRect().left);
    return key === null ? null : (layout.current?.items.get(key) ?? null);
  };
  return (
    <canvas
      ref={canvas}
      aria-hidden="true"
      data-testid={testId}
      className="absolute inset-0 size-full"
      onPointerMove={(event) => {
        const item = itemAt(event);
        if ((item?.selection === last.current) || (item === null && last.current === null)) return;
        last.current = item === null ? null : item.selection;
        event.currentTarget.style.cursor = item === null ? "" : "pointer";
        onHit(item);
      }}
      onPointerLeave={() => {
        last.current = null;
        onHit(null);
      }}
      onClick={(event) => {
        const item = itemAt(event);
        if (item !== null) onChoose(item);
      }}
    />
  );
}
