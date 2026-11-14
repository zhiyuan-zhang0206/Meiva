import { onCLS, onFCP, onINP, onLCP } from "web-vitals";

import { flushTelemetry, track } from "./telemetry";

interface MetricValue { value: number }

export interface WebVitalsRuntime {
  hasWindow?: boolean;
  onCLS: (report: (metric: MetricValue) => void) => void;
  onFCP: (report: (metric: MetricValue) => void) => void;
  onINP: (report: (metric: MetricValue) => void) => void;
  onLCP: (report: (metric: MetricValue) => void) => void;
}

const nativeRuntime: WebVitalsRuntime = { onCLS, onFCP, onINP, onLCP };
let initialized = false;
let generation = 0;
let testRuntime: WebVitalsRuntime | null = null;

// meiva:note
// Register once per document, including React remounts and client routing.
// The library owns session windows, interaction ranking, visibility resumption
// and bfcache resets. Its observers live for the document lifetime; unmounting
// the authenticated route must not stop them or register another set.
export function initWebVitals(runtime = testRuntime ?? nativeRuntime): void {
  if (initialized || !(runtime.hasWindow ?? typeof window !== "undefined")) return;
  initialized = true;
  const currentGeneration = generation;
  const report = (key: "fcp" | "lcp" | "cls" | "inp") => (metric: MetricValue) => {
    if (currentGeneration !== generation) return;
    const value = key === "cls"
      ? String(Math.round(metric.value * 10_000) / 10_000)
      : Math.round(metric.value);
    // meiva:note
    // Only the scalar crosses the boundary: never forward entries, targets,
    // navigation URLs or attribution. Preserve the shared buffer/rate limits.
    track("web-vitals", { key, value, dedupe: false });
    // meiva:note
    // Hidden callbacks can run after telemetry's own flush listener, and INP
    // can finish in a queued task. Flush at the actual reporting boundary.
    flushTelemetry();
  };
  runtime.onFCP(report("fcp"));
  runtime.onLCP(report("lcp"));
  runtime.onCLS(report("cls"));
  runtime.onINP(report("inp"));
}

export function webVitalsSetRuntimeForTest(runtime: WebVitalsRuntime | null): void {
  testRuntime = runtime;
}

// meiva:note
// Invalidate test callbacks; upstream has no observer disposal API.
// Tests using the native library need their own document/module environment.
export function webVitalsResetForTest(): void {
  generation++;
  initialized = false;
}
