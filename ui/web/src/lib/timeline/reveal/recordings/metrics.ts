// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md
import { RevealController } from "../controller";
import type { Arrival } from "./traces";

export interface DisplaySample {
  tMs: number;
  shown: number;
  arrived: number;
}

function graphemeCount(text: string): number {
  let n = 0;
  for (const _ of new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text)) n++;
  return n;
}

export function simulateReveal(
  arrivals: Arrival[],
  { frameMs = 1000 / 60, commitDelayMs = 50, maxMs = 120_000 } = {},
): DisplaySample[] {
  const ctrl = new RevealController("");
  const samples: DisplaySample[] = [];
  let text = "";
  let next = 0;
  const commitAt = (arrivals.at(-1)?.tMs ?? 0) + commitDelayMs;
  let committed = false;
  ctrl.resume(0);
  for (let t = 0; t <= maxMs; t += frameMs) {
    let changed = false;
    while (next < arrivals.length && arrivals[next].tMs <= t) {
      text += arrivals[next++].text;
      changed = true;
    }
    if (!committed && t >= commitAt) {
      committed = true;
      changed = true;
    }
    if (changed) ctrl.update(text, !committed, t);
    ctrl.advance(t);
    samples.push({ tMs: t, shown: ctrl.shownCount(), arrived: graphemeCount(text) });
    if (committed && ctrl.done) break;
  }
  return samples;
}

export function rateSeries(samples: DisplaySample[], windowMs: number, spanMs: number, pick: (s: DisplaySample) => number): number[] {
  const rates: number[] = [];
  let idx = 0;
  let prev = 0;
  for (let start = 0; start < spanMs; start += windowMs) {
    let last = prev;
    while (idx < samples.length && samples[idx].tMs < start + windowMs) last = pick(samples[idx++]);
    rates.push(((last - prev) * 1000) / windowMs);
    prev = last;
  }
  return rates;
}

export function coefficientOfVariation(values: number[]): number {
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  if (mean === 0) return 0;
  const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length;
  return Math.sqrt(variance) / mean;
}

export function maxLagMs(samples: DisplaySample[]): number {
  let worst = 0;
  let arrivalIdx = 0;
  for (const s of samples) {
    if (s.arrived <= s.shown) continue;
    while (arrivalIdx < samples.length && samples[arrivalIdx].arrived <= s.shown) arrivalIdx++;
    worst = Math.max(worst, s.tMs - samples[arrivalIdx].tMs);
  }
  return worst;
}

export function longestStallWithBacklogMs(samples: DisplaySample[]): number {
  let worst = 0;
  let stallStart: number | null = null;
  let prevShown = 0;
  for (const s of samples) {
    const backlog = s.arrived > s.shown;
    if (s.shown === prevShown && backlog) stallStart ??= s.tMs;
    else stallStart = null;
    if (stallStart !== null) worst = Math.max(worst, s.tMs - stallStart);
    prevShown = s.shown;
  }
  return worst;
}
