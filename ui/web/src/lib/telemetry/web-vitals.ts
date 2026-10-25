import { flushTelemetry, track } from "./telemetry";
import {
  buildFinalWebVitalsMetrics,
  consumeLayoutShiftAndEventEntries,
  consumeLcpEntries,
  consumePaintEntries,
  createWebVitalsMetricsState,
  resetWebVitalsMetricsState,
} from "./web-vitals-plan";

export interface PerformanceObserverConstructorLike {
  new (callback: PerformanceObserverCallback): PerformanceObserver;
  supportedEntryTypes: readonly string[];
}

export interface WebVitalsRuntime {
  observerConstructor?: PerformanceObserverConstructorLike;
  hasWindow?: boolean;
}

interface MetricObserver {
  observer: PerformanceObserver;
  consume: (entries: PerformanceEntry[], observer: PerformanceObserver) => void;
}

const state = {
  initialized: false,
  finalized: false,
  observers: [] as MetricObserver[],
  metrics: createWebVitalsMetricsState(),
  cleanupFinalizers: null as (() => void) | null,
  testRuntime: null as WebVitalsRuntime | null,
};

function reportMetric(key: "fcp" | "lcp" | "cls" | "inp", value: number | string): void {
  track("web-vitals", { key, value, dedupe: false });
}

function observe(
  type: string,
  consume: MetricObserver["consume"],
  options: { durationThreshold?: number } = {},
  observerCtor?: PerformanceObserverConstructorLike,
): PerformanceObserver | undefined {
  const ObserverCtor =
    observerCtor ??
    state.testRuntime?.observerConstructor ??
    (typeof PerformanceObserver === "undefined" ? undefined : PerformanceObserver);
  if (!ObserverCtor?.supportedEntryTypes.includes(type)) {
    return undefined;
  }
  const observer = new ObserverCtor((list, currentObserver) => {
    consume(list.getEntries(), currentObserver);
  });
  try {
    observer.observe({ type, buffered: true, ...options });
  } catch {
    observer.disconnect();
    return undefined;
  }
  state.observers.push({ observer, consume });
  return observer;
}

function stopObservers(): void {
  for (const { observer } of state.observers) observer.disconnect();
}

function drainObservers(): void {
  for (const { observer, consume } of state.observers) {
    const pending = observer.takeRecords();
    if (pending.length > 0) consume(pending, observer);
  }
}

function detachFinalizers(): void {
  state.cleanupFinalizers?.();
  state.cleanupFinalizers = null;
}

function reportFinalMetrics(): void {
  if (state.finalized) return;
  state.finalized = true;
  drainObservers();
  for (const metric of buildFinalWebVitalsMetrics(state.metrics)) {
    reportMetric(metric.key, metric.value);
  }
  stopObservers();
  detachFinalizers();
  flushTelemetry();
}

/** Start one-shot native Web Vitals observers for the current page load. */
export function initWebVitals(runtime?: WebVitalsRuntime): void {
  const effectiveRuntime = runtime ?? state.testRuntime ?? undefined;
  const hasWindow = effectiveRuntime?.hasWindow ?? typeof window !== "undefined";
  const observerCtor = effectiveRuntime?.observerConstructor;
  if (
    state.initialized ||
    !hasWindow ||
    (observerCtor === undefined && typeof PerformanceObserver === "undefined")
  ) {
    return;
  }
  state.initialized = true;

  observe(
    "paint",
    (entries, observer) => {
      const fcp = consumePaintEntries(state.metrics, entries);
      if (fcp === null) return;
      reportMetric("fcp", fcp);
      observer.disconnect();
    },
    {},
    observerCtor,
  );

  observe(
    "largest-contentful-paint",
    (entries) => {
      consumeLcpEntries(state.metrics, entries);
    },
    {},
    observerCtor,
  );

  state.metrics.collectingLayoutShift =
    observe(
      "layout-shift",
      (entries) => {
        consumeLayoutShiftAndEventEntries(state.metrics, "layout-shift", entries);
      },
      {},
      observerCtor,
    ) !== undefined;

  observe(
    "event",
    (entries) => {
      consumeLayoutShiftAndEventEntries(state.metrics, "event", entries);
    },
    { durationThreshold: 40 },
    observerCtor,
  );

  const onVisibilityChange = () => {
    if (document.visibilityState === "hidden") reportFinalMetrics();
  };
  window.addEventListener("pagehide", reportFinalMetrics);
  document.addEventListener("visibilitychange", onVisibilityChange);
  state.cleanupFinalizers = () => {
    window.removeEventListener("pagehide", reportFinalMetrics);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };
}

export function webVitalsSetRuntimeForTest(runtime: WebVitalsRuntime | null): void {
  state.testRuntime = runtime;
}

export function webVitalsResetForTest(): void {
  stopObservers();
  detachFinalizers();
  state.initialized = false;
  state.finalized = false;
  state.observers = [];
  resetWebVitalsMetricsState(state.metrics);
}
