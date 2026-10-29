// meiva:note
// The counters the zoom work is judged by, each independent of machine load: how many rows were laid out
// again (`relayouts`, and the milliseconds they took in `layoutMs`), how many times the chart's row tree
// rendered (`renders`), how many canvases it painted (`draws`, with `paintMs` and `linksMs` for the time
// they took), and how many small labels followed the viewport (`followers`). They are published on
// `window.__runTimelineStats` only when the page was asked for them before load
// (`window.__runTimelineStatsWanted`), so an ordinary visitor pays a handful of number adds.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md

export interface RunTimelineStats {
  relayouts: number;
  renders: number;
  followers: number;
  draws: number;
  layoutMs: number;
  paintMs: number;
  linksMs: number;
}

declare global {
  interface Window {
    __runTimelineStatsWanted?: boolean;
    __runTimelineStats?: RunTimelineStats;
  }
}

export const runTimelineStats: RunTimelineStats = {
  relayouts: 0,
  renders: 0,
  followers: 0,
  draws: 0,
  layoutMs: 0,
  paintMs: 0,
  linksMs: 0,
};

if (typeof window !== "undefined" && window.__runTimelineStatsWanted === true) {
  window.__runTimelineStats = runTimelineStats;
}

export function countRelayouts(rows: number, ms: number): void {
  runTimelineStats.relayouts += rows;
  runTimelineStats.layoutMs += ms;
}

export function countRender(): void {
  runTimelineStats.renders += 1;
}

export function countFollowerRender(): void {
  runTimelineStats.followers += 1;
}

export function countDraw(): void {
  runTimelineStats.draws += 1;
}

export function countPaintMs(ms: number): void {
  runTimelineStats.paintMs += ms;
}

export function countLinksMs(ms: number): void {
  runTimelineStats.linksMs += ms;
}
