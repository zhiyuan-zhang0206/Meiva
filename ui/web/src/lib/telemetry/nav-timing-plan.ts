const NAV_FALLBACK_MS = 5000;

export interface NavTimingMetrics {
  fallbackMs: number;
  intentAt: number | null;
  intentPath: string | null;
  longestTask: number;
  maxQueueMs: number;
  startedResources: number;
  resourceNames: string[];
  frames: number;
}

export function createNavTimingMetrics(): NavTimingMetrics {
  return {
    fallbackMs: NAV_FALLBACK_MS,
    intentAt: null,
    intentPath: null,
    longestTask: 0,
    maxQueueMs: 0,
    startedResources: 0,
    resourceNames: [],
    frames: 0,
  };
}

export function resetNavWatchMetrics(metrics: NavTimingMetrics): void {
  metrics.longestTask = 0;
  metrics.maxQueueMs = 0;
  metrics.startedResources = 0;
  metrics.resourceNames = [];
  metrics.frames = 0;
}

export function recordLongTaskDuration(metrics: NavTimingMetrics, duration: number): void {
  if (metrics.intentAt === null) return;
  metrics.longestTask = Math.max(metrics.longestTask, duration);
}

export function recordResourceEntry(
  metrics: NavTimingMetrics,
  name: string,
  startTime: number,
  requestStart: number,
): void {
  if (metrics.intentAt === null) return;
  metrics.startedResources += 1;
  if (metrics.resourceNames.length < 4) {
    metrics.resourceNames.push(name.split("/").pop() ?? name);
  }
  const queued = requestStart - startTime;
  if (queued > metrics.maxQueueMs) metrics.maxQueueMs = queued;
}

export function formatNavTiming(commitMs: number, paintMs: number, taskMs: number): string {
  return `c=${Math.round(commitMs)}|p=${Math.round(paintMs)}|t=${Math.round(taskMs)}`;
}

export function formatNavFallbackHead(elapsedMs: number, metrics: NavTimingMetrics): string {
  return `c=${Math.round(elapsedMs)}|n=${metrics.startedResources}|q=${Math.round(metrics.maxQueueMs)}|f=1`;
}
