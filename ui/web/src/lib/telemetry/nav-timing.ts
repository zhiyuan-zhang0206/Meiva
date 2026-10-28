"use client";

// meiva:note
// Navigation timing — measures the user-visible cost of a route change from
// the click that started it to the first frame after the new route commits.
//
// Why two numbers plus the longest task:
//   - commitMs: markNavIntent() -> usePathname() changed (React committed the
//     new route's tree). Includes RSC fetch if any and the synchronous render.
//   - paintMs:  markNavIntent() -> the first animation frame after the commit.
//     The gap between commit and paint is layout + paint + any passive effects
//     that blocked the frame.
//   - taskMs:   the longest LongTask observed between intent and 400ms after
//     paint (late query resolves / deferred mounts). Zero when the browser has
//     no LongTask observer or no task crossed the 50ms floor.
//
// All three ride in one `nav-timing` event's ≤64-char `value` (`c=...|p=...|t=...`)
// so a single row in `frontend_telemetry` tells whether a slow switch was
// network/render before commit, frame work at commit, or a post-mount stall.
//
// Fallback: if the App Router transition has not committed within 5s of a
// sidebar click, record `key: "fallback"` with how many resources started, the
// worst connection-queue wait and the frames actually painted while stuck, then
// assign the page's address (through the browser port) so a wedged transition
// still lands on the page.

import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import type { BrowserNavigation } from "@/lib/browser/browser";

import {
  createNavTimingMetrics,
  formatNavFallbackHead,
  formatNavTiming,
  recordLongTaskDuration,
  recordResourceEntry,
  resetNavWatchMetrics,
} from "./nav-timing-plan";
import { track } from "./telemetry";

export { formatNavTiming } from "./nav-timing-plan";

const SETTLE_MS = 400;

const state: {
  metrics: ReturnType<typeof createNavTimingMetrics>;
  frameLoop: number;
  observer: PerformanceObserver | null;
  resources: PerformanceObserver | null;
  fallbackTimer: ReturnType<typeof setTimeout> | null;
} = {
  metrics: createNavTimingMetrics(),
  frameLoop: 0,
  observer: null,
  resources: null,
  fallbackTimer: null,
};

function isResourceTimingEntry(entry: PerformanceEntry): entry is PerformanceResourceTiming {
  return "requestStart" in entry && typeof entry.requestStart === "number";
}

function countFrames(): void {
  state.frameLoop = requestAnimationFrame(() => {
    state.metrics.frames += 1;
    countFrames();
  });
}

function stopFrames(): void {
  if (state.frameLoop !== 0) cancelAnimationFrame(state.frameLoop);
  state.frameLoop = 0;
}

function startWatching(): void {
  resetNavWatchMetrics(state.metrics);
  stopFrames();
  countFrames();
  if (typeof PerformanceObserver === "undefined") return;
  try {
    if (state.observer === null) {
      state.observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          recordLongTaskDuration(state.metrics, entry.duration);
        }
      });
      state.observer.observe({ entryTypes: ["longtask"] });
    }
    if (state.resources === null) {
      state.resources = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (!isResourceTimingEntry(entry)) continue;
          recordResourceEntry(
            state.metrics,
            entry.name,
            entry.startTime,
            entry.requestStart,
          );
        }
      });
      state.resources.observe({ entryTypes: ["resource"] });
    }
  } catch {
    return;
  }
}

function clearFallback(): void {
  if (state.fallbackTimer !== null) clearTimeout(state.fallbackTimer);
  state.fallbackTimer = null;
}

export function setNavFallbackMs(ms: number): void {
  if (Number.isFinite(ms) && ms > 0) state.metrics.fallbackMs = ms;
}

// meiva:note
// A navigation starts here: the caller passes the page's own navigation port, so
// the fallback below can check where the page actually is and send it there
// without this module reading the window.
export function markNavIntent(navigation: BrowserNavigation, href?: string): void {
  state.metrics.intentAt = performance.now();
  state.metrics.intentPath = href ?? null;
  startWatching();
  clearFallback();
  if (state.metrics.intentPath === null) return;
  const target: string = state.metrics.intentPath;
  state.fallbackTimer = setTimeout(() => {
    state.fallbackTimer = null;
    if (navigation.location()?.pathname === target) return;
    const elapsed =
      state.metrics.intentAt === null ? 0 : performance.now() - state.metrics.intentAt;
    stopFrames();
    const page = target.replace(/^\//, "") || "home";
    track("nav-timing", {
      page,
      key: "fallback",
      value:
        formatNavFallbackHead(elapsed, state.metrics) +
        `|fr=${state.metrics.frames}|v=${document.visibilityState === "visible" ? 1 : 0}`,
      dedupe: false,
    });
    track("nav-timing", {
      page,
      key: "res",
      value: state.metrics.resourceNames.join(","),
      dedupe: false,
    });
    navigation.assign(target);
  }, state.metrics.fallbackMs);
}

export function useNavTimingReport(pathnameOverride?: string): void {
  const routerPathname = usePathname();
  const pathname = pathnameOverride ?? (routerPathname || "/");
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (previous.current === null) {
      previous.current = pathname;
      return;
    }
    if (previous.current === pathname) return;
    previous.current = pathname;
    if (state.metrics.intentAt === null) return;
    clearFallback();
    stopFrames();
    const commit = performance.now() - state.metrics.intentAt;
    requestAnimationFrame(() => {
      if (state.metrics.intentAt === null) return;
      const paint = performance.now() - state.metrics.intentAt;
      setTimeout(() => {
        const task = state.metrics.longestTask;
        state.metrics.intentAt = null;
        state.metrics.intentPath = null;
        track("nav-timing", {
          page: pathname.replace(/^\//, "") || "home",
          key: "nav",
          value: formatNavTiming(commit, paint, task),
          dedupe: false,
        });
      }, SETTLE_MS);
    });
  }, [pathname]);
}
