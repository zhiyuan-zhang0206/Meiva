// meiva:note
// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { fakeBrowser } from "@/lib/browser/testing";
import { initTelemetry, telemetryTestControl, track } from "./telemetry";
import { initWebVitals, webVitalsResetForTest } from "./web-vitals";

afterEach(() => {
  webVitalsResetForTest();
  telemetryTestControl.reset();
});

it("delivers late library callbacks after the transport already flushed", () => {
  const send = vi.fn(() => true);
  initTelemetry({ ...fakeBrowser(), beacon: { send } });
  track("api-timing", { key: "status", value: 900, dedupe: false });
  document.dispatchEvent(new Event("visibilitychange"));
  let report: ((metric: { value: number }) => void) | undefined;
  initWebVitals({
    onFCP: () => undefined, onLCP: () => undefined, onINP: () => undefined,
    onCLS: (cb) => { report = cb; },
  });
  // meiva:note
  // Include private metadata to prove the adapter sends only the scalar.
  const metric = { value: 0.123456, entries: [{ target: "private" }], navigationURL: "/private" };
  report?.(metric);
  const batches = send.mock.calls as unknown as [string, string][];
  const events = batches.flatMap(([, body]) => (JSON.parse(body) as { events: Record<string, unknown>[] }).events);
  expect(events).toEqual(expect.arrayContaining([
    expect.objectContaining({ element: "web-vitals", key: "cls", value: "0.1235" }),
  ]));
  expect(JSON.stringify(events)).not.toContain("private");
  expect(telemetryTestControl.flush()).toBe(0);
});

it("the native library tolerates the test browser without supported timing APIs", () => {
  expect(() => initWebVitals()).not.toThrow();
  expect(telemetryTestControl.bufferSize()).toBe(0);
});
