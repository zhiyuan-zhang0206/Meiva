import { endIn, type LinkRow, type ResolvedLink } from "./resolve";

// meiva:note
// The key of the neighbour of `current` in `row` (sorted by time), or null past either end.
export function stepLink(row: readonly ResolvedLink[], current: string, dir: "left" | "right"): string | null {
  const at = row.findIndex((l) => l.key === current);
  if (at < 0) return null;
  const to = at + (dir === "left" ? -1 : 1);
  return to < 0 ? null : (row.at(to)?.key ?? null);
}

// meiva:note
// The link of `row` nearest in time to `ms`.
export function nearestLink(links: readonly ResolvedLink[], row: LinkRow, ms: number): string | null {
  let best: { key: string; gap: number } | null = null;
  for (const l of links) {
    const gap = Math.abs((endIn(l, row)?.ms ?? Infinity) - ms);
    if (best === null || gap < best.gap) best = { key: l.key, gap };
  }
  return best?.key ?? null;
}

// meiva:note
// A cubic Bezier between two points; its control points sit level with the ends, pulled sideways.
export interface Curve {
  key: string;
  x0: number;
  y0: number;
  c1x: number;
  c1y: number;
  c2x: number;
  c2y: number;
  x1: number;
  y1: number;
}

const BEND_MIN_PX = 24;
const BEND_MAX_PX = 60;
const BEND_PER_DY = 0.45;
const STAGGER_STEPS = 5;

function stagger(key: string): number {
  let h = 0;
  for (let i = 0; i < key.length; i += 1) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return 0.76 + (h % STAGGER_STEPS) * 0.12;
}

export function curveOf(key: string, x0: number, y0: number, x1: number, y1: number): Curve {
  const base = Math.min(Math.max(Math.abs(y1 - y0) * BEND_PER_DY, BEND_MIN_PX), BEND_MAX_PX);
  const pull = base * stagger(key);
  return { key, x0, y0, c1x: x0 + pull, c1y: y0, c2x: x1 + pull, c2y: y1, x1, y1 };
}

export function curvePoint(c: Curve, t: number): { x: number; y: number } {
  const u = 1 - t;
  const w0 = u ** 3;
  const w1 = 3 * u * u * t;
  const w2 = 3 * u * t * t;
  const w3 = t ** 3;
  return { x: w0 * c.x0 + w1 * c.c1x + w2 * c.c2x + w3 * c.x1, y: w0 * c.y0 + w1 * c.c1y + w2 * c.c2y + w3 * c.y1 };
}

export function endTangent(c: Curve): { x: number; y: number } {
  const dx = c.x1 - c.c2x;
  const dy = c.y1 - c.c2y;
  const len = Math.hypot(dx, dy);
  return len === 0 ? { x: 0, y: 1 } : { x: dx / len, y: dy / len };
}

const SAMPLES = 24;

function segmentDistance(px: number, py: number, a: { x: number; y: number }, b: { x: number; y: number }): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const t = len2 === 0 ? 0 : Math.min(Math.max(((px - a.x) * dx + (py - a.y) * dy) / len2, 0), 1);
  return Math.hypot(px - (a.x + t * dx), py - (a.y + t * dy));
}

export function distanceToCurve(c: Curve, px: number, py: number): number {
  let best = Infinity;
  let prev = curvePoint(c, 0);
  for (let i = 1; i <= SAMPLES; i += 1) {
    const next = curvePoint(c, i / SAMPLES);
    best = Math.min(best, segmentDistance(px, py, prev, next));
    prev = next;
  }
  return best;
}
