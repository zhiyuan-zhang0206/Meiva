"use client";

// meiva:note
// One row's canvas: sized to its track in device pixels, redrawn once per animation frame after a
// render that changed something it draws (`paintKey`); a render that changed nothing visible costs
// no paint. A pointer layer turns a position into the item under it (`hitTest`).
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md

import { useEffect, useMemo, useRef } from "react";

import { buildHitIndex, hitTest, type RowLayout } from "../model/timeline-canvas-hit";
import { resolveColor } from "./resolve-color";
import type { PaintCtx } from "./run-timeline-paint";

export function TrackCanvas({
  width,
  height,
  layout,
  paint,
  paintKey,
  testId,
  onHit,
  onChoose,
}: {
  /** The track's width in CSS pixels. */
  width: number;
  height: number;
  layout: RowLayout;
  paint: (p: PaintCtx) => void;
  paintKey: string;
  testId: string;
  /** The item under the pointer (its key) or null; fired when it changes. */
  onHit: (key: string | null) => void;
  onChoose: (key: string) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const index = useMemo(() => buildHitIndex(layout), [layout]);
  const last = useRef<string | null>(null);

  const painted = useRef<string | null>(null);
  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d") ?? null;
    if (el === null || ctx === null) return;
    if (painted.current === paintKey) return;
    const frame = requestAnimationFrame(() => {
      const dpr = window.devicePixelRatio || 1;
      const pixelW = Math.max(1, Math.round(width * dpr));
      const pixelH = Math.max(1, Math.round(height * dpr));
      if (el.width !== pixelW) el.width = pixelW;
      if (el.height !== pixelH) el.height = pixelH;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const style = getComputedStyle(el);
      paint({
        ctx,
        resolve: resolveColor,
        width,
        height,
        dpr,
        font: style.fontFamily || "sans-serif",
        trackBg: "color-mix(in srgb, var(--muted) 40%, var(--card))",
      });
      painted.current = paintKey;
    });
    return () => cancelAnimationFrame(frame);
  });

  const keyAt = (event: React.PointerEvent | React.MouseEvent) =>
    hitTest(index, event.clientX - event.currentTarget.getBoundingClientRect().left);
  return (
    <canvas
      ref={canvas}
      aria-hidden="true"
      data-testid={testId}
      className="absolute inset-0 size-full"
      onPointerMove={(event) => {
        const key = keyAt(event);
        if (key === last.current) return;
        last.current = key;
        event.currentTarget.style.cursor = key === null ? "" : "pointer";
        onHit(key);
      }}
      onPointerLeave={() => {
        last.current = null;
        onHit(null);
      }}
      onClick={(event) => {
        const key = keyAt(event);
        if (key !== null) onChoose(key);
      }}
    />
  );
}
