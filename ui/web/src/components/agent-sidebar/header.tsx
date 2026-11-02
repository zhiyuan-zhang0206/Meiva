"use client";

import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";


// ── Floating search overlay ──
//
// Task #723: search moved from an inline sidebar box to a floating overlay —
// a dimmed backdrop + a centered, shadowed panel (command-palette style), so
// search stays reachable even when the sidebar is collapsed. Task #750 (user
// ruling): the overlay is fully independent — the sidebar list is NOT filtered
// while typing; the query lives in the store only so the overlay input keeps
// its text across open/close. Results are agent-id DESCENDING. Picking a

export function SidebarHeader({
  trailing,
}: {
  trailing: React.ReactNode;
}) {
  return (
    <header
      className={cn(
        "relative items-center justify-between gap-1.5 px-2.5 py-2",
        BAR_DIVIDER_CLASS,
        BAR_HEIGHT_CLASS,
        FLEX
      )}
    >
      <h1 className={cn("truncate text-base font-semibold tracking-wide", MIN_W_0)}>Meiva</h1>
      <div className={cn("shrink-0 items-center gap-1", FLEX)}>{trailing}</div>
    </header>
  );
}
