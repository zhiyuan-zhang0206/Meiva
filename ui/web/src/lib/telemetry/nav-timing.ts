"use client";

// meiva:note
// Where a navigation's seconds actually go, measured in the reviewer's own
// browser, plus a floor under the worst case. Measured on 2026-10-20: a click on
// Control reported click-to-URL of 15.2 s and 17.9 s with a longest main-thread
// task of ZERO - the router issued its RSC request (answered in 6 ms), then made
// no request at all and committed nothing for fifteen seconds while the main
// thread stayed idle. That is a navigation that never gets to run, not a slow
// one, so after NAV_FALLBACK_MS without a commit this performs a real location
// change: a full load costs ~1.5 s on the machine that reported the stall, which
// bounds the worst case instead of leaving it unbounded.
//
// The floor is 5000 ms: click-to-commit measures p50 69 / p95 84 / p99 106
// unthrottled (30 navigations, preview build, ubuntu host, 2026-10-20), p99 332 at
// 4x CPU throttle and 655 at 8x, so 5000 is ~47x the unthrottled p99 and ~7.6x the
// slowest healthy commit measured. It is deliberately far above any healthy
// navigation: everything faster than it is a real measurement, so the fallback
// never rewrites a slow-but-fine commit into a full reload, and the only rows it
// produces are genuine non-commits. A real hang costs 5000 ms plus the ~0.6 s load
// that follows. The user can override it with behavior.navFallbackMs.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

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

const state = {
  metrics: createNavTimingMetrics(),
  frameLoop: 0,
  observer: null as PerformanceObserver | null,
  resources: null as PerformanceObserver | null,
  fallbackTimer: null as ReturnType<typeof setTimeout> | null,
  requestFrame: null as ((cb: FrameRequestCallback) => number) | null,
  cancelFrame: null as ((id: number) => void) | null,
};

function countFrames(): void {
  const req = state.requestFrame ?? requestAnimationFrame;
  state.frameLoop = req(() => {
    state.metrics.frames += 1;
    countFrames();
  });
}

function stopFrames(): void {
  const cancel = state.cancelFrame ?? cancelAnimationFrame;
  if (state.frameLoop !== 0) cancel(state.frameLoop);
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
        for (const entry of list.getEntries() as PerformanceResourceTiming[]) {
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

export function markNavIntent(href?: string): void {
  if (typeof window === "undefined") return;
  state.metrics.intentAt = performance.now();
  state.metrics.intentPath = href ?? null;
  startWatching();
  clearFallback();
  if (state.metrics.intentPath === null) return;
  const target: string = state.metrics.intentPath;
  state.fallbackTimer = setTimeout(() => {
    state.fallbackTimer = null;
    if (window.location.pathname === target) return;
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
    window.location.assign(target);
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
    const req = state.requestFrame ?? requestAnimationFrame;
    req(() => {
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

export const navTimingTestControl = Object.freeze({
  setFrameScheduler(
    scheduler: {
      requestAnimationFrame: (cb: FrameRequestCallback) => number;
      cancelAnimationFrame: (id: number) => void;
    } | null,
  ): void {
    state.requestFrame = scheduler?.requestAnimationFrame ?? null;
    state.cancelFrame = scheduler?.cancelAnimationFrame ?? null;
  },
});
