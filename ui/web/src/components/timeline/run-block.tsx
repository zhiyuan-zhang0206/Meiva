"use client";

// meiva:note
// Unified collapsible TurnBlock for secondary agent steps (both live "Working…"
// turns and completed "Worked for Xs" turns): inline unboxed header with live/settled
// duration summary and a sticky expanded header. An expanded body always flows at
// its natural height in the timeline - there is no inner scrollport and no height
// cap - so a finished block is as tall as it really was and a running one extends
// downward from a fixed top edge while the reader scrolls the timeline itself.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md

import { ChevronRight, Layers } from "lucide-react";
import { useTranslations } from "next-intl";
import { memo, type ReactNode } from "react";

import { formatDuration, type SdkCall } from "@/lib/format/item-summary";
import { cn } from "@/lib/format/utils";
import {
  BAR_STICKY_TOP_CLASS,
  FLEX,
  FLEX_1,
  FLEX_COL,
  MIN_H_0,
  MIN_W_0,
  OVERFLOW_HIDDEN,
} from "@/lib/layout/layout";

import { CallBadge } from "./view/card-summary";
import { useNow } from "./model/reasoning-clock";
import { formatTurnSummary, turnSummaryEqual, type TurnSummary } from "./model/runs";

interface TurnBlockProps {
  id: string;
  toggleKey?: string;
  memberIds: readonly string[];
  memberRanks?: readonly string[];
  memberSources?: readonly string[];
  summary: TurnSummary;
  expanded: boolean;
  onToggle: (id: string, currentlyExpanded: boolean, button: HTMLButtonElement) => void;
  turnActive?: boolean;
  liveWindow?: boolean;
  children?: ReactNode;
  timelineSource?: "buffer" | "canonical";
  displayRank?: number;
  // meiva:note
  // Page-clock time of the thread's last received event; anchors the live clock while no block is
  // in flight. 0 = unknown, and the clock falls back to the in-flight block alone.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  lastEventAt?: number;
}

function sameIds(a: readonly string[], b: readonly string[]): boolean {
  return a === b || (a.length === b.length && a.every((id, index) => id === b[index]));
}

function sameOptionalIds(a?: readonly string[], b?: readonly string[]): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return sameIds(a, b);
}

function turnBlockPropsEqual(prev: TurnBlockProps, next: TurnBlockProps): boolean {
  return (
    prev.id === next.id &&
    prev.toggleKey === next.toggleKey &&
    prev.expanded === next.expanded &&
    prev.onToggle === next.onToggle &&
    prev.turnActive === next.turnActive &&
    prev.lastEventAt === next.lastEventAt &&
    prev.liveWindow === next.liveWindow &&
    prev.children === next.children &&
    prev.timelineSource === next.timelineSource &&
    prev.displayRank === next.displayRank &&
    sameIds(prev.memberIds, next.memberIds) &&
    sameOptionalIds(prev.memberRanks, next.memberRanks) &&
    sameOptionalIds(prev.memberSources, next.memberSources) &&
    turnSummaryEqual(prev.summary, next.summary)
  );
}

export const TurnBlock = memo(function TurnBlock({
  id,
  toggleKey,
  memberIds,
  memberRanks,
  memberSources,
  summary,
  expanded,
  onToggle,
  turnActive,
  liveWindow = false,
  children,
  timelineSource = "canonical",
  displayRank,
  lastEventAt = 0,
}: TurnBlockProps) {
  const t = useTranslations("timeline");
  const liveBlockStartedAt =
    turnActive && summary.lastLiveKind != null && summary.lastLiveStartedAt > 0
      ? summary.lastLiveStartedAt
      : 0;
  // meiva:note
  // The live "Working for" anchor is the in-flight block's start while one runs, and the page's own
  // time of the thread's last received event when none does — the tick then keeps counting the
  // current stretch instead of standing on the measured total, which is what used to freeze the
  // header between blocks (a tool result committed, the model thinking before its next token, a
  // harness step transition: user report 2026-11-08, stuck at "Working for 1m 2s" with no event
  // left to advance it). Anchoring on the last received event, not the turn's start, keeps a turn
  // that spans an idle gap (a restart, a wake-up) from counting that gap as work.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  const liveAnchor =
    liveBlockStartedAt > 0 ? liveBlockStartedAt : turnActive ? lastEventAt : 0;
  const liveNow = useNow(Boolean(turnActive) && liveAnchor > 0);

  const liveDelta = turnActive && liveAnchor > 0 ? Math.max(liveNow - liveAnchor, 0) : 0;
  const totalWorkedMs = summary.workedMs + liveDelta;

  const hasWork = summary.thinking > 0 || summary.code > 0 || summary.output > 0;
  const hasLiveTimer =
    (summary.lastLiveKind !== null && summary.lastLiveStartedAt > 0) || liveAnchor > 0;
  const workedLabel = turnActive
    ? totalWorkedMs > 0 || hasWork || hasLiveTimer
      ? t("workingFor", { duration: formatDuration(totalWorkedMs) })
      : t("working")
    : hasWork
      ? t("workedFor", { duration: formatDuration(summary.workedMs) })
      : t("workActivity");

  const actionSummary = expanded ? formatTurnSummary(summary, (key, values) =>
    t(key as Parameters<typeof t>[0], values),
  ) : "";

  const hasSdkCalls = summary.sdkCalls.length > 0;

  // meiva:note
  // Keep collapsed turn strips single-line by rendering aggregated SDK call chips
  // only when the TurnBlock is expanded.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md
  const sdkChips = expanded && hasSdkCalls ? (
    <span className={cn("flex-wrap items-center gap-x-1.5 gap-y-0.5 pl-5 text-[11px] font-mono opacity-75", FLEX)}>
      {summary.sdkCalls.map((c, i) => (
        <TurnCallChip key={c.method} call={c} showDot={i > 0} />
      ))}
    </span>
  ) : null;


  return (
    <div
      data-item-id={id}
      data-timeline-source={timelineSource}
      data-display-rank={displayRank}
      data-turn-id={id}
      data-turn-expanded={expanded}
      data-turn-member-ids={memberIds.join(" ")}
      data-turn-member-ranks={memberRanks?.join(" ")}
      data-turn-member-sources={memberSources?.join(" ")}
      data-live-activity-window={liveWindow ? "true" : undefined}
      aria-live="off"
      className="relative"
    >
      <button
        type="button"
        onClick={(event) => onToggle(toggleKey ?? id, expanded, event.currentTarget)}
        className={cn(
          "group/turn-toggle w-[calc(100%+0.5rem)] -ml-2 px-2 py-0.5 items-start gap-1.5 rounded-md bg-background text-left text-muted-foreground select-none",
          FLEX,
          expanded && cn("sticky z-20", BAR_STICKY_TOP_CLASS),
        )}
        aria-expanded={expanded}
        data-testid="turn-toggle"
        data-expanded={expanded}
      >
        <ChevronRight
          aria-hidden="true"
          className={cn(
            "size-3.5 shrink-0 mt-0.5",
            expanded
              ? "rotate-90 opacity-60"
              : "opacity-0 group-hover/turn-toggle:opacity-60 group-focus-visible/turn-toggle:opacity-60 pointer-coarse:opacity-60",
          )}
        />
        <span className={cn("gap-0.5", FLEX, FLEX_COL, MIN_W_0, FLEX_1)}>
          <span className={cn("items-center gap-1.5 text-[13px] leading-snug", FLEX, MIN_W_0)}>
            <Layers className="size-3.5 shrink-0 opacity-70" />
            <span className="truncate whitespace-nowrap tabular-nums group-hover/turn-toggle:text-foreground group-focus-visible/turn-toggle:text-foreground">
              {workedLabel}
            </span>
          </span>
          {actionSummary && (
            <span className="pl-5 text-xs leading-snug">{actionSummary}</span>
          )}
          {sdkChips}
        </span>
      </button>
      <div
        className={cn(
          "grid",
          expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
      >
        <div className={cn(OVERFLOW_HIDDEN, MIN_H_0, MIN_W_0)}>
          {expanded ? (
            <div className="mt-1.5 ml-1.5 pl-2 pr-2 space-y-1">
              {children}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}, turnBlockPropsEqual);

// meiva:note
// Immediate inline activity row rendered right after human message submission
// before the first reasoning or tool item arrives, matching TurnBlock's header geometry.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md
export const PendingLiveActivityWindow = memo(function PendingLiveActivityWindow() {
  const t = useTranslations("timeline");
  return (
    <div
      data-live-activity-window="true"
      data-testid="pending-live-activity-window"
      aria-live="off"
      className="relative"
    >
      <div
        className={cn(
          "w-[calc(100%+0.5rem)] -ml-2 px-2 py-0.5 items-start gap-1.5 rounded-md text-left select-none text-muted-foreground",
          FLEX,
        )}
      >
        <ChevronRight className="size-3.5 shrink-0 opacity-60 mt-0.5" />
        <span className={cn("items-center gap-1.5 text-[13px] leading-snug", FLEX, MIN_W_0)}>
          <Layers className="size-3.5 shrink-0 opacity-70" />
          <span className="truncate whitespace-nowrap tabular-nums">{t("working")}</span>
        </span>
      </div>
    </div>
  );
});

function TurnCallChip({ call, showDot }: { call: SdkCall; showDot: boolean }) {
  return (
    <span>
      {showDot ? <span className="opacity-50">{"· "}</span> : null}
      <CallBadge call={call} />
    </span>
  );
}
