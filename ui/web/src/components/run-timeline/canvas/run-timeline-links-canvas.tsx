"use client";

// meiva:note
// The arrows between agents: one canvas over the whole chart, redrawn on the frame after a render that
// changed one of its inputs; a row's track element is looked up once, and `geometryKey` says when a row
// moved. It draws only — the pointer events stay with the rows below.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/agent-links.meiva.okf.md

import { useEffect, useRef, type RefObject } from "react";

import { bucketOf, clusterToMax, type Arrow, type Cluster } from "../model/links/cluster";
import { curveOf, curvePoint, endTangent, type Curve } from "../model/links/geometry";
import { hitLink } from "../model/links/selection";
import { LINK_COLORS, type ResolvedLink } from "../model/links/resolve";
import { resolveColor } from "./resolve-color";
import type { AxisMap } from "../model/timeline-model-axis";
import type { Viewport } from "../model/timeline-model-viewport";

const ROW_TESTID = { units: "run-timeline-row-units", user: "run-timeline-row-user", other: "run-timeline-row-other" } as const;
const HEAD_PX = 7;
/** How far from a curve a pointer still counts as on it. */
export const LINK_HIT_PX = 4;

/** `(clientX, clientY)` to the arrow under that point, or null. */
export type LinkHit = (clientX: number, clientY: number) => LinkHitResult | null;

interface Box {
  left: number;
  width: number;
  mid: number;
}


function trackOf(canvas: HTMLCanvasElement, end: ResolvedLink["from"], known: Map<string, Element | null>): Element | null {
  const id = `${end.row}:${end.agent}`;
  const cached = known.get(id);
  if (cached?.isConnected) return cached;
  const scope = end.row === "units" ? `[data-testid="agent-view-agent-${end.agent}"]` : `[data-testid="agent-view-${end.row}"]`;
  const track = canvas.parentElement?.querySelector(`${scope} [data-testid="${ROW_TESTID[end.row]}"] [data-track]`) ?? null;
  known.set(id, track);
  return track;
}

function boxOf(canvas: HTMLCanvasElement, end: ResolvedLink["from"], known: Map<string, Element | null>): Box | null {
  const track = trackOf(canvas, end, known);
  if (track == null) return null;
  const rect = track.getBoundingClientRect();
  const origin = canvas.getBoundingClientRect();
  return { left: rect.left - origin.left, width: rect.width, mid: rect.top - origin.top + rect.height / 2 };
}


interface Painted {
  links: readonly ResolvedLink[];
  axis: AxisMap;
  from: number;
  to: number;
  maxArrows: number;
  selected: ReadonlySet<string>;
  hovered: ReadonlySet<string>;
  geometry: string;
}

/** The arrow under the pointer: its key (a link's own, or a merged group's) and every link it stands for. */
export interface LinkHitResult {
  key: string;
  members: readonly string[];
}

const BADGE_FONT_PX = 10;
const BADGE_AT = 0.2;
// meiva:note
// Where a badge may sit, in order of preference: just after the arrow leaves its start row, then a step
// further along or one line up or down, so badges stay near their own arrow.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/agent-links.meiva.okf.md
const BADGE_SLOTS: readonly (readonly [number, number])[] = [
  [0.2, 0], [0.26, 0], [0.32, 0], [0.2, -11], [0.26, -11], [0.32, -11],
  [0.2, 11], [0.26, 11], [0.32, 11], [0.38, 0], [0.38, -11], [0.38, 11],
];
// One width for every arrow, merged or not; a hovered or selected one is a little thicker.
const WIDTH_PX = 1.6;

/** A cheap fingerprint of where the arrows are: equal when nothing moved, so the merge need not be searched again. */
function arrowsSignature(arrows: readonly Arrow[], max: number): string {
  let h0 = 0;
  let h1 = 0;
  for (const a of arrows) {
    h0 = (h0 * 31 + Math.round(a.x0 * 10) + a.key.length) % 1_000_000_007;
    h1 = (h1 * 37 + Math.round(a.x1 * 10) + a.bucket.length) % 1_000_000_007;
  }
  return `${max}|${arrows.length}|${h0}|${h1}`;
}

export function LinksCanvas({
  links,
  selectedKeys,
  hoverKeys,
  axis,
  view,
  maxArrows,
  geometryKey,
  hitRef,
}: {
  /** The arrows to draw (the kinds that are switched on). */
  links: readonly ResolvedLink[];
  /** The links selected / hovered: an arrow is lit when it stands for any of them. */
  selectedKeys: ReadonlySet<string>;
  hoverKeys: ReadonlySet<string>;
  axis: AxisMap;
  view: Viewport;
  maxArrows: number;
  geometryKey: string;
  hitRef: RefObject<LinkHit>;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const cache = useRef<{ signature: string; clusters: Cluster[] } | null>(null);
  const painted = useRef<Painted | null>(null);
  const tracks = useRef(new Map<string, Element | null>());
  const badgeWidths = useRef(new Map<string, number>());

  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d") ?? null;
    if (el === null || ctx === null) return;
    const last = painted.current;
    if (
      last !== null &&
      last.links === links &&
      last.axis === axis &&
      last.from === view.from &&
      last.to === view.to &&
      last.maxArrows === maxArrows &&
      last.selected === selectedKeys &&
      last.hovered === hoverKeys &&
      last.geometry === geometryKey
    ) {
      return;
    }
    const frame = requestAnimationFrame(() => {
      const rect = el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const pixelW = Math.max(1, Math.round(rect.width * dpr));
      const pixelH = Math.max(1, Math.round(rect.height * dpr));
      if (el.width !== pixelW) el.width = pixelW;
      if (el.height !== pixelH) el.height = pixelH;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const shown = axis.viewU(view);
      const span = Math.max(shown.to - shown.from, 1e-9);
      const boxes = new Map<string, ReturnType<typeof boxOf>>();
      const place = (end: ResolvedLink["from"]) => {
        const id = `${end.row}:${end.agent}`;
        if (!boxes.has(id)) boxes.set(id, boxOf(el, end, tracks.current));
        const box = boxes.get(id) ?? null;
        const frac = (axis.toU(end.ms) - shown.from) / span;
        return box === null || frac < 0 || frac > 1 ? null : { x: box.left + box.width * frac, y: box.mid };
      };
      const arrows: Arrow[] = [];
      const ys = new Map<string, [number, number]>();
      const byKey = new Map<string, ResolvedLink>();
      for (const l of links) {
        const a = place(l.from);
        const b = place(l.to);
        if (a === null || b === null) continue;
        arrows.push({ key: l.key, bucket: bucketOf(l), x0: a.x, x1: b.x });
        ys.set(l.key, [a.y, b.y]);
        byKey.set(l.key, l);
      }
      // Arrows close together on screen are drawn as one with a count, merged only as far as it takes to
      // stay within the limit; zooming in leaves fewer to merge, so they fall apart.
      // The search runs again only when the arrows moved (a zoom, a pan, a resize); hovering and
      // selecting repaint over the same result.
      const signature = arrowsSignature(arrows, maxArrows);
      if (cache.current?.signature !== signature) {
        cache.current = { signature, clusters: clusterToMax(arrows, maxArrows, rect.width * 2).clusters };
      }
      const clusters = cache.current.clusters;
      const curves: Curve[] = [];
      const sizes = new Map<string, number>();
      const kinds = new Map<string, ResolvedLink["kind"]>();
      const members = new Map<string, readonly string[]>();
      for (const c of clusters) {
        const [y0, y1] = ys.get(c.members[0]) ?? [0, 0];
        curves.push(curveOf(c.key, c.x0, y0, c.x1, y1));
        sizes.set(c.key, c.members.length);
        members.set(c.key, c.members);
        const kind = byKey.get(c.members[0])?.kind;
        if (kind !== undefined) kinds.set(c.key, kind);
      }
      const litOf = (key: string): 0 | 1 | 2 => {
        const keys = members.get(key) ?? [];
        return keys.some((k) => selectedKeys.has(k)) ? 2 : keys.some((k) => hoverKeys.has(k)) ? 1 : 0;
      };
      const strokeOf = (kind: ResolvedLink["kind"]) => resolveColor(LINK_COLORS[kind]);
      const widthOf = (label: string): number => {
        const known = badgeWidths.current.get(label);
        if (known !== undefined) return known;
        ctx.font = `600 ${BADGE_FONT_PX}px sans-serif`;
        const width = Math.max(ctx.measureText(label).width + 8, 16);
        badgeWidths.current.set(label, width);
        return width;
      };
      // meiva:note
      // A badge that would cover one already placed moves along its own arrow, so two arrows close
      // together keep both counts readable.
      const placedBadges: { x0: number; y0: number; x1: number; y1: number }[] = [];
      const badgeAt = new Map<string, { x: number; y: number; width: number }>();
      for (const rank of [2, 1, 0]) {
        for (const c of curves) {
          if (litOf(c.key) !== rank) continue;
          const count = sizes.get(c.key) ?? 1;
          if (count <= 1) continue;
          const width = widthOf(String(count));
          let spot: { x: number; y: number } | null = null;
          for (const [at, dy] of BADGE_SLOTS) {
            const on = curvePoint(c, at);
            const point = { x: on.x, y: on.y + dy };
            const box = { x0: point.x - width / 2 - 1, y0: point.y - 9, x1: point.x + width / 2 + 1, y1: point.y + 9 };
            if (placedBadges.some((o) => box.x0 < o.x1 && o.x0 < box.x1 && box.y0 < o.y1 && o.y0 < box.y1)) continue;
            placedBadges.push(box);
            spot = point;
            break;
          }
          // meiva:note
          // Arrows packed tighter than the slots allow keep their count too: an overlapping badge is worse
          // than a clear one, but a missing count is worst of all.
          const point = spot ?? curvePoint(c, BADGE_AT);
          if (spot === null) {
            placedBadges.push({ x0: point.x - width / 2 - 1, y0: point.y - 9, x1: point.x + width / 2 + 1, y1: point.y + 9 });
          }
          badgeAt.set(c.key, { x: point.x, y: point.y, width });
        }
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const rank of [0, 1, 2]) {
        for (const c of curves) {
          const lit = litOf(c.key);
          if (lit !== rank) continue;
          const kind = kinds.get(c.key);
          if (kind === undefined) continue;
          const count = sizes.get(c.key) ?? 1;
          const color = strokeOf(kind);
          ctx.globalAlpha = lit > 0 ? 1 : selectedKeys.size > 0 ? 0.45 : 0.9;
          ctx.strokeStyle = color;
          ctx.fillStyle = color;
          ctx.lineWidth = lit === 2 ? WIDTH_PX + 1.2 : lit === 1 ? WIDTH_PX + 0.7 : WIDTH_PX;
          ctx.beginPath();
          ctx.moveTo(c.x0, c.y0);
          ctx.bezierCurveTo(c.c1x, c.c1y, c.c2x, c.c2y, c.x1, c.y1);
          ctx.stroke();
          const tan = endTangent(c);
          const bx = c.x1 - tan.x * HEAD_PX;
          const by = c.y1 - tan.y * HEAD_PX;
          ctx.globalAlpha = 1;
          ctx.beginPath();
          ctx.moveTo(c.x1, c.y1);
          ctx.lineTo(bx - tan.y * HEAD_PX * 0.55, by + tan.x * HEAD_PX * 0.55);
          ctx.lineTo(bx + tan.y * HEAD_PX * 0.55, by - tan.x * HEAD_PX * 0.55);
          ctx.closePath();
          ctx.fill();
          const badge = count > 1 ? badgeAt.get(c.key) : undefined;
          if (badge !== undefined) {
            // The count, in a badge on the arrow just after it leaves its start row, clear of the rows between.
            ctx.font = `600 ${BADGE_FONT_PX}px sans-serif`;
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.roundRect(badge.x - badge.width / 2, badge.y - 8, badge.width, 16, 8);
            ctx.fill();
            ctx.fillStyle = "#ffffff";
            ctx.fillText(String(count), badge.x, badge.y);
          }
        }
      }
      ctx.globalAlpha = 1;
      painted.current = {
        links,
        axis,
        from: view.from,
        to: view.to,
        maxArrows,
        selected: selectedKeys,
        hovered: hoverKeys,
        geometry: geometryKey,
      };
      hitRef.current = (clientX, clientY) => {
        const now = el.getBoundingClientRect();
        const key = hitLink(curves, clientX - now.left, clientY - now.top, LINK_HIT_PX);
        return key === null ? null : { key, members: members.get(key) ?? [key] };
      };
    });
    return () => cancelAnimationFrame(frame);
  });
  return <canvas ref={canvas} aria-hidden="true" data-testid="run-timeline-links-canvas" className="pointer-events-none absolute inset-0 z-10 size-full" />;
}
