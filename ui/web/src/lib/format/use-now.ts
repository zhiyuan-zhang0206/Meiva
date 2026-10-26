"use client";

import { useEffect, useState } from "react";

// A page-level ticking clock for values derived from absolute timestamps
// (runtime from created_at, TTL remaining from expires_at): one lightweight
// interval keeps them live between server polls without adding a polling
// system of its own. The tick lives in the component that renders those
// values, so an unmounted surface (a closed panel, a navigated-away page)
// leaves no interval running.
export function useNow(intervalMs: number, initialNow: Date): Date {
  const [now, setNow] = useState(initialNow);
  useEffect(() => {
    const id = setInterval(
      () => setNow((prev) => new Date(prev.getTime() + intervalMs)),
      intervalMs,
    );
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
