import { DEFAULT_CLOCK_FORMAT, formatClockText, type ClockFormat } from "@/lib/format/time";

import type { TimelineWindow } from "./timeline-model-selection";

// meiva:note
// The visible part of the loaded data on the time axis, in epoch milliseconds.
export interface Viewport {
  from: number;
  to: number;
}

// meiva:note
// The narrowest viewport a zoom may reach: enough to separate neighbouring message units.
export const MIN_VIEW_MS = 100;

export function viewportOf(window: TimelineWindow): Viewport {
  return { from: Date.parse(window.from), to: Date.parse(window.to) };
}

// meiva:note
// Keeps a viewport of unchanged span inside the base extent; one wider than the base becomes the base.
export function clampViewport(view: Viewport, base: Viewport): Viewport {
  const span = view.to - view.from;
  if (span >= base.to - base.from) return base;
  const from = Math.min(Math.max(view.from, base.from), base.to - span);
  return { from, to: from + span };
}

// meiva:note
// Scales the viewport span by `factor` (below 1 zooms in) keeping the instant at `frac` (0..1 across the view) fixed.
export function zoomViewport(
  view: Viewport,
  base: Viewport,
  frac: number,
  factor: number,
  minSpan: number = MIN_VIEW_MS,
): Viewport {
  const span = view.to - view.from;
  const floor = Math.min(minSpan, base.to - base.from);
  const next = Math.min(Math.max(span * factor, floor), base.to - base.from);
  const anchor = view.from + Math.min(Math.max(frac, 0), 1) * span;
  const from = anchor - Math.min(Math.max(frac, 0), 1) * next;
  return clampViewport({ from, to: from + next }, base);
}

// meiva:note
// Shifts the viewport by `fraction` of its own span (positive = later), staying inside the base extent.
export function panViewport(view: Viewport, base: Viewport, fraction: number): Viewport {
  const delta = (view.to - view.from) * fraction;
  return clampViewport({ from: view.from + delta, to: view.to + delta }, base);
}

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const TICK_STEPS = [
  1, 2, 5, 10, 20, 50, 100, 200, 500, SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND, 15 * SECOND,
  30 * SECOND, MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE, HOUR,
  2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY,
];

export interface AxisTick {
  // meiva:note
  // Position across the viewport, 0..100.
  left: number;
  label: string;
}

function tickLabel(ms: number, step: number, clockFormat: ClockFormat): string {
  const d = new Date(ms);
  const clock = formatClockText(d, { clockFormat });
  if (step >= DAY) return `${d.getMonth() + 1}/${d.getDate()}`;
  if (step >= MINUTE) return step >= HOUR * 6 ? `${d.getMonth() + 1}/${d.getDate()} ${clock}` : clock;
  if (step >= SECOND) return formatClockText(d, { clockFormat, seconds: true });
  return formatClockText(d, { clockFormat, milliseconds: true });
}

// meiva:note
// Round-number ticks (local time) across the viewport, at most about `target` of them.
export function axisTicks(
  view: Viewport,
  target = 6,
  clockFormat: ClockFormat = DEFAULT_CLOCK_FORMAT,
): AxisTick[] {
  const span = view.to - view.from;
  if (!(span > 0)) return [];
  const step = TICK_STEPS.find((candidate) => span / candidate <= target) ?? DAY;
  // meiva:note
  // Day-and-longer steps align to local midnight, shorter ones to the local clock.
  const offset = step >= HOUR ? new Date(view.from).getTimezoneOffset() * MINUTE : 0;
  const ticks: AxisTick[] = [];
  for (let at = Math.ceil((view.from - offset) / step) * step + offset; at <= view.to; at += step) {
    ticks.push({ left: ((at - view.from) / span) * 100, label: tickLabel(at, step, clockFormat) });
  }
  return ticks;
}
