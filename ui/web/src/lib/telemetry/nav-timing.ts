"use client";

// meiva:note
// Where a navigation's seconds actually go, measured in the reviewer's own
// browser: from the click (markNavIntent) to the URL changing, to the first
// frame after it, plus the longest main-thread task inside that window. One
// telemetry event per navigation, so "clicking Settings took ten seconds" can be
// answered with numbers from the machine it happened on instead of a clean-room
// guess (user report 2026-10-20).
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import { track } from "./telemetry";

const SETTLE_MS = 400;

let intentAt: number | null = null;
let longestTask = 0;
let observer: PerformanceObserver | null = null;

function startWatching(): void {
  longestTask = 0;
  if (observer !== null || typeof PerformanceObserver === "undefined") return;
  try {
    observer = new PerformanceObserver((list) => {
      if (intentAt === null) return;
      for (const entry of list.getEntries()) {
        longestTask = Math.max(longestTask, entry.duration);
      }
    });
    observer.observe({ entryTypes: ["longtask"] });
  } catch {
    observer = null;
  }
}

/** Stamp the moment a navigation was requested: put this in the click handler. */
export function markNavIntent(): void {
  if (typeof window === "undefined") return;
  intentAt = performance.now();
  startWatching();
}

export function formatNavTiming(commitMs: number, paintMs: number, taskMs: number): string {
  return `c=${Math.round(commitMs)}|p=${Math.round(paintMs)}|t=${Math.round(taskMs)}`;
}

/** Report the pending navigation, if any, when the route changes. Mount once. */
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
    const commit = performance.now() - intentAt;
    requestAnimationFrame(() => {
      if (intentAt === null) return;
      const paint = performance.now() - intentAt;
      setTimeout(() => {
        const task = longestTask;
        intentAt = null;
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
