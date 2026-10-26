"use client";

import { useEffect, useState } from "react";

// meiva:note
// A page-level ticking clock for values derived from absolute timestamps (runtime from
// created_at, TTL remaining from expires_at): one lightweight interval keeps them live between
// server polls without adding a polling system of its own. The tick lives in the component that
// renders those values, so an unmounted surface (a closed panel, a navigated-away page) leaves
// no interval running.
//
// Every tick reads the wall clock it is given, never the previous tick plus the interval: a
// browser throttles `setInterval` in a background tab, and a clock that adds one interval per
// tick falls behind real time for good.
export function useNow(intervalMs: number, wallNow: () => number): Date {
  const [now, setNow] = useState(() => new Date(wallNow()));
  useEffect(() => {
    const id = setInterval(() => setNow(new Date(wallNow())), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, wallNow]);
  return now;
}
