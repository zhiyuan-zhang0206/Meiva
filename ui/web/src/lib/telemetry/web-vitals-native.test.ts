// meiva:note
// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { createContext, runInContext } from "node:vm";
import { afterEach, expect, it, vi } from "vitest";
import { telemetryTestControl } from "./telemetry";
import { initWebVitals, type WebVitalsRuntime } from "./web-vitals";

interface Observer {
  callback: PerformanceObserverCallback;
  types: string[];
  records: PerformanceEntry[];
}
const observers: Observer[] = [];
class BrowserObserver implements Observer {
  static supportedEntryTypes = ["paint", "largest-contentful-paint", "layout-shift", "event", "first-input"];
  types: string[] = [];
  records: PerformanceEntry[] = [];
  constructor(public callback: PerformanceObserverCallback) { observers.push(this); }
  observe(options: PerformanceObserverInit) { if (options.type) this.types.push(options.type); }
  disconnect() { return; }
  takeRecords() { return this.records.splice(0); }
}
function emit(type: string, entries: object[]) {
  for (const observer of observers.filter((item) => item.types.includes(type))) {
    observer.callback({ getEntries: () => entries } as PerformanceObserverEntryList, observer as unknown as PerformanceObserver);
  }
}
async function settle() { await new Promise((resolve) => setTimeout(resolve, 30)); }
const page = new EventTarget();
const documentState = { visibilityState: "visible" };
function visibility(value: string) {
  documentState.visibilityState = value;
  page.dispatchEvent(new Event("visibilitychange"));
}
afterEach(() => { telemetryTestControl.reset();  });

it("uses the shipped library for CLS windows, INP ranking, hidden resumption and bfcache reset", async () => {
  const track = vi.fn();
  telemetryTestControl.intercept({ onTrack: track, onFlush: () => undefined });
  class EventTiming { interactionId = 0; }
  Object.defineProperty(EventTiming.prototype, "interactionId", { value: 0 });
  // meiva:note
  // A fresh realm supplies browser APIs at construction, keeping upstream's
  // document-lifetime state isolated without patching the host environment.
  const realm = createContext({
    document: documentState,
    performance: { getEntriesByType: () => [], now: () => 0, interactionCount: 50 },
    PerformanceObserver: BrowserObserver,
    PerformanceEventTiming: EventTiming,
    addEventListener: page.addEventListener.bind(page),
    removeEventListener: page.removeEventListener.bind(page),
    setTimeout, clearTimeout, queueMicrotask,
    requestAnimationFrame: (cb: () => void) => setTimeout(cb, 0),
    requestIdleCallback: (cb: () => void) => setTimeout(cb, 0),
    cancelIdleCallback: clearTimeout,
  });
  const require = createRequire(import.meta.url);
  const bundle = join(dirname(require.resolve("web-vitals")), "web-vitals.iife.js");
  runInContext(readFileSync(bundle, "utf8"), realm);
  const runtime = runInContext("webVitals", realm) as WebVitalsRuntime;
  initWebVitals(runtime);
  emit("paint", [{ name: "first-contentful-paint", startTime: 10, entryType: "paint" }]);
  await settle();
  emit("layout-shift", [
    { startTime: 100, value: 0.1, hadRecentInput: false },
    { startTime: 500, value: 0.2, hadRecentInput: false },
    { startTime: 2100, value: 9, hadRecentInput: true },
  ]);
  await settle();
  emit("layout-shift", [{ startTime: 2000, value: 0.25, hadRecentInput: false }]);
  // meiva:note
  // The two slow events belong to one interaction; 50 interactions select
  // the second longest interaction, not the second longest event.
  emit("event", [
    { interactionId: 7, startTime: 100, duration: 900, entryType: "event" },
    { interactionId: 7, startTime: 100, duration: 800, entryType: "event" },
    { interactionId: 14, startTime: 200, duration: 160, entryType: "event" },
  ]);
  await settle();
  visibility("hidden");
  await settle();
  expect(track).toHaveBeenCalledWith("web-vitals", { key: "cls", value: "0.3", dedupe: false });
  expect(track).toHaveBeenCalledWith("web-vitals", { key: "inp", value: 160, dedupe: false });
  visibility("visible");
  emit("layout-shift", [{ startTime: 4000, value: 0.4, hadRecentInput: false }]);
  await settle();
  visibility("hidden");
  await settle();
  expect(track).toHaveBeenCalledWith("web-vitals", { key: "cls", value: "0.4", dedupe: false });
  page.dispatchEvent(new Event("pagehide"));
  visibility("visible");
  const restore = new Event("pageshow");
  Object.defineProperty(restore, "persisted", { value: true });
  page.dispatchEvent(restore);
  await settle();
  emit("layout-shift", [{ startTime: 6000, value: 0.05, hadRecentInput: false }]);
  await settle();
  visibility("hidden");
  await settle();
  expect(track).toHaveBeenCalledWith("web-vitals", { key: "cls", value: "0.05", dedupe: false });
});
