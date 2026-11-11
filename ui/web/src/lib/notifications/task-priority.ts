// meiva:note
// Shared task-priority helpers, so the task board, the task graph and the
// inspector's task button can't drift apart on priority ranking or colors.

import type { TaskPriority } from "../contracts/types";

// meiva:note
// P0 highest .. P3 lowest -- the task board's sort key.
export const PRIORITY_RANK: Record<TaskPriority, number> = {
  P0: 0,
  P1: 1,
  P2: 2,
  P3: 3,
};

// meiva:note
// Badge background by priority (the stakes axis). Callers add their own text
// color (white reads on all four; this theme has no destructive-foreground token).
export const PRIORITY_BG: Record<TaskPriority, string> = {
  P0: "bg-destructive",
  P1: "bg-[#ED8755]",
  P2: "bg-[#2276B5]",
  P3: "bg-slate-500",
};
