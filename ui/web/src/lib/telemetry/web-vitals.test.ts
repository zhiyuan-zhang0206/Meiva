// meiva:note
// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { telemetryTestControl, type TelemetryElement, type TrackOptions } from "./telemetry";
import { initWebVitals, webVitalsResetForTest, webVitalsSetRuntimeForTest, type WebVitalsRuntime } from "./web-vitals";

const track = vi.fn<(element: TelemetryElement, opts?: TrackOptions) => void>();
const flush = vi.fn();
const callbacks = new Map<string, (metric: { value: number }) => void>();
const runtime: WebVitalsRuntime = {
  onFCP: vi.fn((cb: (metric: { value: number }) => void) => { callbacks.set("fcp", cb); }),
  onLCP: vi.fn((cb: (metric: { value: number }) => void) => { callbacks.set("lcp", cb); }),
  onCLS: vi.fn((cb: (metric: { value: number }) => void) => { callbacks.set("cls", cb); }),
  onINP: vi.fn((cb: (metric: { value: number }) => void) => { callbacks.set("inp", cb); }),
};

beforeEach(() => {
  vi.clearAllMocks();
  callbacks.clear();
  webVitalsResetForTest();
  telemetryTestControl.intercept({ onTrack: track, onFlush: flush });
});
afterEach(() => {
  webVitalsResetForTest();
  webVitalsSetRuntimeForTest(null);
  telemetryTestControl.reset();
});

it("registers each standard metric once with the default reporting policy", () => {
  initWebVitals(runtime);
  initWebVitals(runtime);
  for (const register of [runtime.onFCP, runtime.onLCP, runtime.onCLS, runtime.onINP]) {
    expect(register).toHaveBeenCalledTimes(1);
    expect(register).toHaveBeenCalledWith(expect.any(Function));
  }
});

it("forwards only rounded scalars and flushes every library report", () => {
  initWebVitals(runtime);
  callbacks.get("fcp")?.({ value: 456.6 });
  callbacks.get("lcp")?.({ value: 1234.6 });
  callbacks.get("cls")?.({ value: 0.123456 });
  callbacks.get("inp")?.({ value: 310.6 });
  expect(track.mock.calls).toEqual([
    ["web-vitals", { key: "fcp", value: 457, dedupe: false }],
    ["web-vitals", { key: "lcp", value: 1235, dedupe: false }],
    ["web-vitals", { key: "cls", value: "0.1235", dedupe: false }],
    ["web-vitals", { key: "inp", value: 311, dedupe: false }],
  ]);
  expect(flush).toHaveBeenCalledTimes(4);
});

it("accepts updated reports after hidden, pagehide and bfcache restore", () => {
  initWebVitals(runtime);
  callbacks.get("cls")?.({ value: 0.1 });
  window.dispatchEvent(new Event("pagehide"));
  window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
  callbacks.get("cls")?.({ value: 0 });
  callbacks.get("inp")?.({ value: 72 });
  expect(track.mock.calls.map(([, opts]) => opts?.value)).toEqual(["0.1", "0", 72]);
  expect(runtime.onCLS).toHaveBeenCalledTimes(1);
});

it("invalidates stale callbacks when resetting the test environment", () => {
  initWebVitals(runtime);
  const stale = callbacks.get("cls");
  webVitalsResetForTest();
  initWebVitals(runtime);
  stale?.({ value: 1 });
  callbacks.get("cls")?.({ value: 0.2 });
  expect(track).toHaveBeenCalledTimes(1);
});

it("skips registration during SSR", () => {
  initWebVitals({ ...runtime, hasWindow: false });
  expect(callbacks.size).toBe(0);
});
