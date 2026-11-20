// meiva:note
// The counters `render-stats.ts` publishes on `window` when a profiling run asks for them.
import type { RunTimelineStats } from "./render-stats";

declare global {
  interface Window {
    __runTimelineStatsWanted?: boolean;
    __runTimelineStats?: RunTimelineStats;
  }
}

export {};
