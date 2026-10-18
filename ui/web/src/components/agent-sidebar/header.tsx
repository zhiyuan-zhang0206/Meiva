"use client";

import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX } from "@/lib/layout/layout";
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
        "relative items-center justify-between px-3 py-2",
        BAR_DIVIDER_CLASS,
        BAR_HEIGHT_CLASS,
        FLEX
      )}
    >
      <h1 className="text-base font-semibold tracking-wide">Meiva</h1>
      <div className={cn("items-center gap-1.5", FLEX)}>{trailing}</div>
    </header>
  );
}
