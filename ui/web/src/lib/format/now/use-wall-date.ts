"use client";

import { useWallNow } from "@/lib/transport/transport-context";

// meiva:note
// The page's clock as one `Date` for this render: a relative-time label ("3m ago") is computed
// against the wall clock the root hands in (`useWallNow`), not the global one, so a test states the
// instant and the label follows it. It is read on every render, which is exactly how stale each
// label was when the sites called `new Date()` themselves; a surface that must keep counting
// between renders uses `useNow(intervalMs, wallNow)` instead. Do not put the returned `Date` in a
// `useMemo`/`useEffect` dependency array: it is a new object on every render, so the dependency
// would never match.
export function useWallDate(): Date {
  const wallNow = useWallNow();
  return new Date(wallNow());
}
