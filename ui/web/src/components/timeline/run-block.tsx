"use client";

// meiva:note
// Unified collapsible TurnBlock for secondary agent steps (both live "Working…"
// turns and completed "Worked for Xs" turns): inline unboxed header with live/settled
// duration summary, no sticky header pinning, and an inner auto-hiding ScrollArea
// capped at half the visible timeline viewport height (--timeline-turn-max-h, fallback 50dvh).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md

import { ChevronRight, Layers } from "lucide-react";
import { useTranslations } from "next-intl";
import {
  memo,
  type ReactNode,
  type UIEvent as ReactUIEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { ScrollArea } from "@/components/ui/scroll-area";
import { formatDuration, type SdkCall } from "@/lib/format/item-summary";
import { cn } from "@/lib/format/utils";
import {
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
import { windowForSizes } from "./model/use-timeline-window";

const INNER_BOOTSTRAP_ROWS = 24;
const INNER_ESTIMATED_ROW_PX = 40;

interface TurnBlockProps {
  id: string;
  memberIds: readonly string[];
  memberRanks?: readonly string[];
  memberSources?: readonly string[];
  summary: TurnSummary;
  expanded: boolean;
  onToggle: (id: string, currentlyExpanded: boolean) => void;
  turnActive?: boolean;
  liveWindow?: boolean;
  onActivityAtBottomChange?: (atBottom: boolean) => void;
  scrollToBottomSeq?: number;
  windowedCount?: number;
  turnRows?: number;
  renderWindowedRow?: (index: number) => ReactNode;
  children?: ReactNode;
  timelineSource?: "buffer" | "canonical";
  displayRank?: number;
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
    prev.expanded === next.expanded &&
    prev.onToggle === next.onToggle &&
    prev.turnActive === next.turnActive &&
    prev.liveWindow === next.liveWindow &&
    prev.onActivityAtBottomChange === next.onActivityAtBottomChange &&
    prev.scrollToBottomSeq === next.scrollToBottomSeq &&
    prev.windowedCount === next.windowedCount &&
    prev.turnRows === next.turnRows &&
    prev.renderWindowedRow === next.renderWindowedRow &&
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
  memberIds,
  memberRanks,
  memberSources,
  summary,
  expanded,
  onToggle,
  turnActive,
  liveWindow = false,
  onActivityAtBottomChange,
  scrollToBottomSeq = 0,
  windowedCount,
  turnRows,
  renderWindowedRow,
  children,
  timelineSource = "canonical",
  displayRank,
}: TurnBlockProps) {
  const t = useTranslations("timeline");
  const liveBlockStartedAt =
    turnActive && summary.lastLiveKind != null && summary.lastLiveStartedAt > 0
      ? summary.lastLiveStartedAt
      : 0;
  const liveNow = useNow(liveBlockStartedAt > 0);

  // meiva:note
  // Inner scrollport management: tracks detached scroll state on the active
  // trailing turn for the outer ScrollToBottomButton, and bounds DOM mounts
  // inside mega-turns exceeding MEIVA_TIMELINE_WINDOW_TURN_ROWS.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
  const activityScrollRef = useRef<HTMLDivElement | null>(null);
  const activityContentRef = useRef<HTMLDivElement | null>(null);
  const innerStickyRef = useRef(true);
  const lastInnerScrollTopRef = useRef(0);
  const seenScrollSeqRef = useRef(scrollToBottomSeq);

  const isWindowed =
    expanded &&
    windowedCount !== undefined &&
    turnRows !== undefined &&
    windowedCount > turnRows &&
    renderWindowedRow !== undefined;
  const [innerView, setInnerView] = useState({ top: 0, height: 0 });

  const handleInnerScroll = useCallback(
    (event: ReactUIEvent<HTMLDivElement>) => {
      const el = activityScrollRef.current;
      if (!el) return;
      if (event.target !== el && event.target !== event.currentTarget) return;

      if (isWindowed) {
        setInnerView((prev) =>
          Math.abs(prev.top - el.scrollTop) >= 1 || prev.height !== el.clientHeight
            ? { top: el.scrollTop, height: el.clientHeight }
            : prev,
        );
      }

      if (!liveWindow) return;
      const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
      const prevTop = lastInnerScrollTopRef.current;
      lastInnerScrollTopRef.current = el.scrollTop;

      if (dist <= 16) {
        if (!innerStickyRef.current) innerStickyRef.current = true;
        onActivityAtBottomChange?.(true);
      } else if (el.scrollTop < prevTop - 1) {
        innerStickyRef.current = false;
        onActivityAtBottomChange?.(false);
      }
    },
    [isWindowed, liveWindow, onActivityAtBottomChange],
  );

  useLayoutEffect(() => {
    if (!isWindowed) return;
    const el = activityScrollRef.current;
    if (!el) return;
    setInnerView((prev) =>
      prev.top !== el.scrollTop || prev.height !== el.clientHeight
        ? { top: el.scrollTop, height: el.clientHeight }
        : prev,
    );
  }, [isWindowed]);

  useEffect(() => {
    if (!liveWindow || !expanded) {
      innerStickyRef.current = true;
      onActivityAtBottomChange?.(true);
      return;
    }
    return () => {
      onActivityAtBottomChange?.(true);
    };
  }, [liveWindow, expanded, onActivityAtBottomChange]);

  useEffect(() => {
    if (seenScrollSeqRef.current === scrollToBottomSeq) return;
    seenScrollSeqRef.current = scrollToBottomSeq;
    if (!liveWindow || !expanded) return;
    const el = activityScrollRef.current;
    if (!el) return;
    innerStickyRef.current = true;
    onActivityAtBottomChange?.(true);
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [scrollToBottomSeq, liveWindow, expanded, onActivityAtBottomChange]);

  useLayoutEffect(() => {
    if (!liveWindow || !expanded) return;
    const scroller = activityScrollRef.current;
    const content = activityContentRef.current;
    if (!scroller) return;
    const syncToBottom = () => {
      if (!innerStickyRef.current) return;
      scroller.scrollTop = scroller.scrollHeight;
      lastInnerScrollTopRef.current = scroller.scrollTop;
    };
    syncToBottom();
    if (!content || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(syncToBottom);
    ro.observe(content);
    return () => ro.disconnect();
  }, [liveWindow, expanded, children, windowedCount]);

  const liveDelta = liveBlockStartedAt > 0 ? Math.max(liveNow - liveBlockStartedAt, 0) : 0;
  const totalWorkedMs = summary.workedMs + liveDelta;

  const hasWork = summary.thinking > 0 || summary.code > 0 || summary.output > 0;
  const hasLiveTimer = summary.lastLiveKind !== null && summary.lastLiveStartedAt > 0;
  const workedLabel = turnActive
    ? totalWorkedMs > 0 || hasWork || hasLiveTimer
      ? t("workingFor", { duration: formatDuration(totalWorkedMs) })
      : t("working")
    : hasWork && summary.workedMs > 0
      ? t("workedFor", { duration: formatDuration(summary.workedMs) })
      : null;

  const actionSummary = formatTurnSummary(summary, (key, values) =>
    t(key as Parameters<typeof t>[0], values),
  );

  const headerParts: string[] = [];
  if (workedLabel) headerParts.push(workedLabel);
  if (actionSummary) headerParts.push(actionSummary);

  const hasSdkCalls = summary.sdkCalls.length > 0;

  // meiva:note
  // Keep collapsed turn strips single-line by rendering aggregated SDK call chips
  // only when the TurnBlock is expanded.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
  const sdkChips = expanded && hasSdkCalls ? (
    <span className={cn("flex-wrap items-center gap-x-1.5 gap-y-0.5 pl-5 text-[11px] font-mono opacity-75", FLEX)}>
      {summary.sdkCalls.map((c, i) => (
        <TurnCallChip key={c.method} call={c} showDot={i > 0} />
      ))}
    </span>
  ) : null;

  const renderedBody = (() => {
    if (!expanded) return null;
    if (!isWindowed) return children;
    const sizes = Array.from({ length: windowedCount }, () => INNER_ESTIMATED_ROW_PX);
    const range =
      innerView.height <= 0 && innerView.top <= 0
        ? (() => {
            const start = Math.max(0, windowedCount - INNER_BOOTSTRAP_ROWS);
            return {
              start,
              end: windowedCount,
              before: start > 0 ? start * (INNER_ESTIMATED_ROW_PX + 12) - 12 : 0,
              after: 0,
            };
          })()
        : windowForSizes(sizes, innerView.top, innerView.height > 0 ? innerView.height : 360, 0);
    return (
      <>
        {range.before > 0 ? (
          <div data-timeline-spacer="turn-before" style={{ height: range.before }} aria-hidden="true" />
        ) : null}
        {Array.from({ length: range.end - range.start }, (_, offset) =>
          renderWindowedRow(range.start + offset),
        )}
        {range.after > 0 ? (
          <div data-timeline-spacer="turn-after" style={{ height: range.after }} aria-hidden="true" />
        ) : null}
      </>
    );
  })();

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
        onClick={() => onToggle(id, expanded)}
        className={cn(
          "items-start gap-1.5 text-left select-none transition-colors",
          expanded
            ? "w-[calc(100%+0.5rem)] -ml-2 px-2 py-0.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/50"
            : "max-w-full rounded-lg px-2 py-0.5 -ml-2 text-muted-foreground/75 hover:text-foreground hover:bg-muted/50",
          FLEX,
        )}
        aria-expanded={expanded}
        data-testid="turn-toggle"
        data-expanded={expanded}
      >
        <ChevronRight
          className={cn(
            "size-3.5 shrink-0 opacity-60 mt-0.5 transition-transform duration-200 motion-reduce:transition-none",
            expanded && "rotate-90",
          )}
        />
        <span className={cn("gap-0.5", FLEX, FLEX_COL, MIN_W_0, expanded && FLEX_1)}>
          <span className={cn("items-center gap-1.5 text-[13px] leading-snug", FLEX, MIN_W_0)}>
            <Layers className="size-3.5 shrink-0 opacity-70" />
            <span className="truncate whitespace-nowrap tabular-nums">{headerParts.join(" · ") || ""}</span>
          </span>
          {sdkChips}
        </span>
      </button>
      <div
        className={cn(
          "grid transition-[grid-template-rows] duration-200 ease-out motion-reduce:transition-none",
          expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
      >
        <div className={cn(OVERFLOW_HIDDEN, MIN_H_0, MIN_W_0)}>
          {expanded ? (
            <ScrollArea
              data-turn-scroll="true"
              data-activity-scroll={liveWindow ? "true" : undefined}
              viewportTestId={liveWindow ? "activity-scroll-viewport" : "turn-scroll-viewport"}
              viewportRef={activityScrollRef}
              onScrollCapture={handleInnerScroll}
              className="mt-1.5"
              viewportClassName="[overflow-anchor:none] max-h-[var(--timeline-turn-max-h,50dvh)]"
            >
              <div ref={activityContentRef} className="ml-1.5 pl-3 pr-2 border-l border-border/60 space-y-1">
                {renderedBody}
              </div>
            </ScrollArea>
          ) : null}
        </div>
      </div>
    </div>
  );
}, turnBlockPropsEqual);

// meiva:note
// Immediate inline activity row rendered right after human message submission
// before the first reasoning or tool item arrives, matching TurnBlock's header geometry.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
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
          "max-w-full items-start gap-1.5 px-2 py-0.5 -ml-2 rounded-md text-left select-none text-muted-foreground",
          FLEX,
        )}
      >
        <ChevronRight className="size-3.5 shrink-0 opacity-60 mt-0.5 rotate-90" />
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
