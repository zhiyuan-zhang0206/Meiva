// Shared helpers for the "waiting on you" queue (the open notices that need a
// response — meiva.ui.notify(require_response=True)), so the queue, the fleet tree
// badge, and the sidebar tree badge can't drift apart on priority ranking or
// colors.

import type { OpenNotice } from "../contracts/types";

// P0 highest .. P3 lowest -- the queue's primary sort key.
export const PRIORITY_RANK: Record<OpenNotice["priority"], number> = {
  P0: 0,
  P1: 1,
  P2: 2,
  P3: 3,
};

// Badge background by priority (the stakes axis). Callers add their own text
// color (white reads on all four; this theme has no destructive-foreground token).
export const PRIORITY_BG: Record<OpenNotice["priority"], string> = {
  P0: "bg-destructive",
  P1: "bg-amber-500",
  P2: "bg-sky-600",
  P3: "bg-slate-500",
};
