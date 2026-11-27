"use client";

// meiva:note
// Unified collapsible TurnBlock for secondary agent steps (both live "Working for Xs"
// turns and completed "Worked for Xs" turns): an unboxed header with a live/settled
// duration summary that sticks below the conversation bar while expanded. An expanded
// body always flows at its natural height in the timeline - there is no inner
// scrollport and no height cap - so a finished block is as tall as it really
// was and a running one extends downward from a fixed top edge while the reader
// scrolls the timeline itself.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md

import { Layers } from "lucide-react";
import { useTranslations } from "next-intl";
import { memo, useState, type ReactNode } from "react";

import { formatWholeSecondsDuration, type SdkCall } from "@/lib/format/item-summary";
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
import { useSyncedShimmer } from "@/components/ui/use-synced-shimmer";
import { useWallNow } from "@/lib/transport/transport-context";

import { CallBadge, DisclosureChevron, DISCLOSURE_TITLE_CLS } from "./view/card-summary/card-summary";
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
  // Page-clock fallback when a running work block's live segment has no timestamped items yet (0 = unknown).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  lastEventAt?: number;
}

function useLiveAnchor(active: boolean, knownAnchor: number | null): number | null {
  const wallNow = useWallNow();
  const [activeSince, setActiveSince] = useState<number | null>(() => (active ? wallNow() : null));
  if (active && activeSince === null) setActiveSince(wallNow());
  if (!active && activeSince !== null) setActiveSince(null);
  if (!active) return null;
  return knownAnchor ?? activeSince ?? wallNow();
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
  const titleRef = useSyncedShimmer(Boolean(turnActive));
  // meiva:note
  // Active-segment work duration (user ruling 2026-11-22): Working = prior completed active
  // segments + (now - live segment start), excluding inter-turn idle gaps; Worked = sum of
  // active segments containing agent output. The live clock never goes quiet (user report
  // 2026-11-25): `priorActiveMs` is a lower bound, and a missing live segment start falls back to
  // `lastEventAt`, then to the instant this header first saw the turn active.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  const liveAnchor = useLiveAnchor(
    Boolean(turnActive),
    summary.liveSegmentStartMs ?? (lastEventAt > 0 ? lastEventAt : null),
  );
  const liveNow = useNow(Boolean(turnActive));

  const hasWork = summary.thinking > 0 || summary.code > 0 || summary.output > 0;
  const liveDuration =
    liveAnchor !== null
      ? formatWholeSecondsDuration(summary.priorActiveMs + Math.max(liveNow - liveAnchor, 0))
      : null;
  const settledDuration =
    !turnActive && hasWork && summary.settledActiveMs !== null
      ? formatWholeSecondsDuration(summary.settledActiveMs)
      : null;
  const workedLabel = liveDuration !== null
    ? t("workingFor", { duration: liveDuration })
    : hasWork
      ? settledDuration !== null
        ? t("workedFor", { duration: settledDuration })
        : t("worked")
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
          "group/disclosure w-[calc(100%+0.5rem)] -ml-2 px-2 items-start gap-1.5 bg-background text-left text-muted-foreground select-none",
          FLEX,
          expanded ? cn("sticky z-20 py-2.5 rounded-none", BAR_STICKY_TOP_CLASS) : "py-0.5 rounded-md",
        )}
        aria-expanded={expanded}
        data-testid="turn-toggle"
        data-turn-header
        data-expanded={expanded}
      >
        <span className={cn(expanded ? "gap-1" : "gap-0.5", FLEX, FLEX_COL, MIN_W_0, FLEX_1)}>
          <span className={cn("items-center gap-1.5 text-[13px] leading-snug", FLEX, MIN_W_0)}>
            <Layers className="size-3.5 shrink-0 opacity-70" />
            <span
              ref={titleRef}
              className={cn(
                DISCLOSURE_TITLE_CLS,
                "whitespace-nowrap tabular-nums",
                turnActive && "timeline-title-working",
              )}
            >
              {workedLabel}
            </span>
            <DisclosureChevron expanded={expanded} />
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
            <div className="mt-1 ml-1.5 pl-2 pr-2 space-y-1">
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
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
// meiva:link ui/web/src/docs/frontend-components/timeline-view/loading-ring.md
export const PendingLiveActivityWindow = memo(function PendingLiveActivityWindow({
  startedAt = 0,
}: {
  startedAt?: number;
}) {
  const t = useTranslations("timeline");
  const titleRef = useSyncedShimmer(true);
  const liveAnchor = useLiveAnchor(true, startedAt > 0 ? startedAt : null);
  const liveNow = useNow(true);
  const label = t("workingFor", {
    duration: formatWholeSecondsDuration(Math.max(liveNow - (liveAnchor ?? liveNow), 0)),
  });
  return (
    <div
      data-live-activity-window="true"
      data-testid="pending-live-activity-window"
      role="status"
      className="relative"
    >
      <div
        className={cn(
          "w-[calc(100%+0.5rem)] -ml-2 px-2 py-0.5 items-start gap-1.5 rounded-md text-left select-none text-muted-foreground",
          FLEX,
        )}
      >
        <span className={cn("items-center gap-1.5 text-[13px] leading-snug", FLEX, FLEX_1, MIN_W_0)}>
          <Layers className="size-3.5 shrink-0 opacity-70" />
          <span ref={titleRef} className="truncate whitespace-nowrap tabular-nums timeline-title-working">
            {label}
          </span>
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
