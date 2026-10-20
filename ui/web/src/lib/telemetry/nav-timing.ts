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
// The floor is 2000 ms because click-to-commit measures p50 69 / p95 84 / p99 106
// unthrottled (30 navigations, preview build, ubuntu host, 2026-10-20), p99 332 at
// 4x CPU throttle and 655 at 8x: 2000 is ~19x the unthrottled p99 and ~1.8x the
// slowest healthy commit measured, so a slow client is not mis-fired. The user can
// override it with behavior.navFallbackMs.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import { track } from "./telemetry";

const SETTLE_MS = 400;
const NAV_FALLBACK_MS = 2000;
let fallbackMs = NAV_FALLBACK_MS;

let intentAt: number | null = null;
let intentPath: string | null = null;
let longestTask = 0;
let maxQueueMs = 0;
let startedResources = 0;
let resourceNames: string[] = [];
let frames = 0;
let frameLoop = 0;
let observer: PerformanceObserver | null = null;
let resources: PerformanceObserver | null = null;
let fallbackTimer: ReturnType<typeof setTimeout> | null = null;

function countFrames(): void {
  frames += 1;
  frameLoop = requestAnimationFrame(countFrames);
}

function stopFrames(): void {
  if (frameLoop !== 0) cancelAnimationFrame(frameLoop);
  frameLoop = 0;
}

function startWatching(): void {
  longestTask = 0;
  maxQueueMs = 0;
  startedResources = 0;
  resourceNames = [];
  frames = 0;
  stopFrames();
  frameLoop = requestAnimationFrame(countFrames);
  if (typeof PerformanceObserver === "undefined") return;
  try {
    if (observer === null) {
      observer = new PerformanceObserver((list) => {
        if (intentAt === null) return;
        for (const entry of list.getEntries()) {
          longestTask = Math.max(longestTask, entry.duration);
        }
      });
      observer.observe({ entryTypes: ["longtask"] });
    }
    if (resources === null) {
      resources = new PerformanceObserver((list) => {
        if (intentAt === null) return;
        for (const entry of list.getEntries() as PerformanceResourceTiming[]) {
          startedResources += 1;
          if (resourceNames.length < 4) {
            resourceNames.push(entry.name.split("/").pop() ?? entry.name);
          }
          const queued = entry.requestStart - entry.startTime;
          if (queued > maxQueueMs) maxQueueMs = queued;
        }
      });
      resources.observe({ entryTypes: ["resource"] });
    }
  } catch {
    return;
  }
}

function clearFallback(): void {
  if (fallbackTimer !== null) clearTimeout(fallbackTimer);
  fallbackTimer = null;
}

export function setNavFallbackMs(ms: number): void {
  if (Number.isFinite(ms) && ms > 0) fallbackMs = ms;
}

export function markNavIntent(href?: string): void {
  if (typeof window === "undefined") return;
  intentAt = performance.now();
  intentPath = href ?? null;
  startWatching();
  clearFallback();
  if (intentPath === null) return;
  const target: string = intentPath;
  fallbackTimer = setTimeout(() => {
    fallbackTimer = null;
    if (window.location.pathname === target) return;
    const elapsed = intentAt === null ? 0 : performance.now() - intentAt;
    stopFrames();
    const page = target.replace(/^\//, "") || "home";
    track("nav-timing", {
      page,
      key: "fallback",
      value:
        `c=${Math.round(elapsed)}|n=${startedResources}|q=${Math.round(maxQueueMs)}|f=1` +
        `|fr=${frames}|v=${document.visibilityState === "visible" ? 1 : 0}`,
      dedupe: false,
    });
    track("nav-timing", {
      page,
      key: "res",
      value: resourceNames.join(","),
      dedupe: false,
    });
    window.location.assign(target);
  }, fallbackMs);
}

export function formatNavTiming(commitMs: number, paintMs: number, taskMs: number): string {
  return `c=${Math.round(commitMs)}|p=${Math.round(paintMs)}|t=${Math.round(taskMs)}`;
}

export function useNavTimingReport(): void {
  const pathname = usePathname();
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (previous.current === null) {
      previous.current = pathname;
      return;
    }
    if (previous.current === pathname) return;
    previous.current = pathname;
    if (intentAt === null) return;
    clearFallback();
    stopFrames();
    const commit = performance.now() - intentAt;
    requestAnimationFrame(() => {
      if (intentAt === null) return;
      const paint = performance.now() - intentAt;
      setTimeout(() => {
        const task = longestTask;
        intentAt = null;
        intentPath = null;
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
