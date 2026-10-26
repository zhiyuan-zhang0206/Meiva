export interface LayoutShiftEntryLike extends PerformanceEntry {
  value: number;
  hadRecentInput: boolean;
}

export interface InteractionTimingEntryLike extends PerformanceEntry {
  interactionId: number;
}

export interface WebVitalsMetricsState {
  fcpReported: boolean;
  latestLcp: number | undefined;
  cumulativeLayoutShift: number;
  collectingLayoutShift: boolean;
  interactionToNextPaint: number | undefined;
}

export interface FinalWebVitalsMetric {
  key: "lcp" | "cls" | "inp";
  value: number | string;
}

export function createWebVitalsMetricsState(): WebVitalsMetricsState {
  return {
    fcpReported: false,
    latestLcp: undefined,
    cumulativeLayoutShift: 0,
    collectingLayoutShift: false,
    interactionToNextPaint: undefined,
  };
}

export function resetWebVitalsMetricsState(metrics: WebVitalsMetricsState): void {
  metrics.fcpReported = false;
  metrics.latestLcp = undefined;
  metrics.cumulativeLayoutShift = 0;
  metrics.collectingLayoutShift = false;
  metrics.interactionToNextPaint = undefined;
}

export function consumePaintEntries(
  metrics: WebVitalsMetricsState,
  entries: readonly PerformanceEntry[],
): number | null {
  if (metrics.fcpReported) return null;
  const fcp = entries.find((candidate) => candidate.name === "first-contentful-paint");
  if (fcp === undefined) return null;
  metrics.fcpReported = true;
  return Math.round(fcp.startTime);
}

export function consumeLcpEntries(
  metrics: WebVitalsMetricsState,
  entries: readonly PerformanceEntry[],
): void {
  for (const candidate of entries) {
    metrics.latestLcp = candidate.startTime;
  }
}

function isLayoutShiftEntry(entry: PerformanceEntry): entry is LayoutShiftEntryLike {
  return (
    "hadRecentInput" in entry &&
    typeof entry.hadRecentInput === "boolean" &&
    "value" in entry &&
    typeof entry.value === "number"
  );
}

function isInteractionTimingEntry(entry: PerformanceEntry): entry is InteractionTimingEntryLike {
  return "interactionId" in entry;
}

export function consumeLayoutShiftAndEventEntries(
  metrics: WebVitalsMetricsState,
  kind: "layout-shift" | "event",
  entries: readonly PerformanceEntry[],
): void {
  if (kind === "layout-shift") {
    for (const candidate of entries) {
      if (isLayoutShiftEntry(candidate) && !candidate.hadRecentInput) {
        metrics.cumulativeLayoutShift += candidate.value;
      }
    }
    return;
  }
  for (const candidate of entries) {
    if (!isInteractionTimingEntry(candidate) || typeof candidate.interactionId !== "number" || candidate.interactionId <= 0) {
      continue;
    }
    metrics.interactionToNextPaint = Math.max(
      metrics.interactionToNextPaint ?? 0,
      candidate.duration,
    );
  }
}

export function buildFinalWebVitalsMetrics(
  metrics: WebVitalsMetricsState,
): FinalWebVitalsMetric[] {
  const result: FinalWebVitalsMetric[] = [];
  if (metrics.latestLcp !== undefined) {
    result.push({ key: "lcp", value: Math.round(metrics.latestLcp) });
  }
  if (metrics.collectingLayoutShift) {
    const stableScore = Math.round(metrics.cumulativeLayoutShift * 10_000) / 10_000;
    result.push({ key: "cls", value: String(stableScore) });
  }
  if (metrics.interactionToNextPaint !== undefined) {
    result.push({ key: "inp", value: Math.round(metrics.interactionToNextPaint) });
  }
  return result;
}
