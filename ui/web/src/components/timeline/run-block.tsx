"use client";

// meiva:note
// Unified collapsible TurnBlock for secondary agent steps (both live "Working…"
// turns and completed "Worked for Xs" turns): inline unboxed header with live/settled
// duration summary and a sticky expanded header. Historical bodies flow through
// the timeline; only the trailing turn retains a half-viewport inner ScrollArea.
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
  BAR_STICKY_TOP_CLASS,
  FLEX,
  FLEX_1,
  FLEX_COL,
  MIN_H_0,
  MIN_W_0,
  OVERFLOW_HIDDEN,
  TIMELINE_TURN_ROW_GAP_PX,
} from "@/lib/layout/layout";

import { CallBadge } from "./view/card-summary";
import { useNow } from "./model/reasoning-clock";
import { formatTurnSummary, turnSummaryEqual, type TurnSummary } from "./model/runs";
import { span, windowForSizes } from "./model/use-timeline-window";

const INNER_BOOTSTRAP_ROWS = 24;
const INNER_ESTIMATED_ROW_PX = 40;
const INNER_UNMEASURED_VIEW_PX = 360;

interface TurnBlockProps {
  id: string;
  toggleKey?: string;
  memberIds: readonly string[];
  memberRanks?: readonly string[];
  memberSources?: readonly string[];
  summary: TurnSummary;
  expanded: boolean;
  bounded?: boolean;
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
  // meiva:note
  // Page-clock time of the thread's last received event; anchors the live clock while no block is
  // in flight. 0 = unknown, and the clock falls back to the in-flight block alone.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.meiva.okf.md
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
    prev.bounded === next.bounded &&
    prev.onToggle === next.onToggle &&
    prev.turnActive === next.turnActive &&
    prev.lastEventAt === next.lastEventAt &&
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
  toggleKey,
  memberIds,
  memberRanks,
  memberSources,
  summary,
  expanded,
  bounded = true,
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
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.meiva.okf.md
  const liveAnchor =
    liveBlockStartedAt > 0 ? liveBlockStartedAt : turnActive ? lastEventAt : 0;
  const liveNow = useNow(Boolean(turnActive) && liveAnchor > 0);

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
    bounded &&
    expanded &&
    windowedCount !== undefined &&
    turnRows !== undefined &&
    windowedCount > turnRows &&
    renderWindowedRow !== undefined;
  // meiva:note
  // The inner window places rows by measured heights (estimates only until a row
  // has mounted once), and while the live window follows its bottom the range is
  // the tail of those sizes, not a mapping of the DOM scrollTop. Mapping a pinned
  // scrollTop through estimates mounted a different row set each frame, the
  // height change re-pinned the scrollport, and the live window flickered for as
  // long as the agent worked. Row gaps follow the shared work-block geometry.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/bounded-window.meiva.okf.md
  const [innerView, setInnerView] = useState({ top: 0, height: 0, follow: true });
  const [rowHeights, setRowHeights] = useState<ReadonlyMap<string, number>>(() => new Map());
  const rowHeightsRef = useRef(rowHeights);
  const rowObserverRef = useRef<{ observer: ResizeObserver; nodes: Set<HTMLElement> } | null>(null);

  const handleInnerScroll = useCallback(
    (event: ReactUIEvent<HTMLDivElement>) => {
      const el = activityScrollRef.current;
      if (!el) return;
      if (event.target !== el && event.target !== event.currentTarget) return;

      const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (liveWindow) {
        const prevTop = lastInnerScrollTopRef.current;
        lastInnerScrollTopRef.current = el.scrollTop;

        if (dist <= 16) {
          if (!innerStickyRef.current) innerStickyRef.current = true;
          onActivityAtBottomChange?.(true);
        } else if (el.scrollTop < prevTop - 1) {
          innerStickyRef.current = false;
          onActivityAtBottomChange?.(false);
        }
      }

      if (isWindowed) {
        const follow = liveWindow && innerStickyRef.current;
        setInnerView((prev) =>
          prev.follow !== follow || prev.height !== el.clientHeight ||
          (!follow && Math.abs(prev.top - el.scrollTop) >= 1)
            ? { top: el.scrollTop, height: el.clientHeight, follow }
            : prev,
        );
      }
    },
    [isWindowed, liveWindow, onActivityAtBottomChange],
  );

  useLayoutEffect(() => {
    if (!isWindowed) return;
    const el = activityScrollRef.current;
    if (!el) return;
    const follow = liveWindow && innerStickyRef.current;
    setInnerView((prev) =>
      prev.top !== el.scrollTop || prev.height !== el.clientHeight || prev.follow !== follow
        ? { top: el.scrollTop, height: el.clientHeight, follow }
        : prev,
    );
  }, [isWindowed, liveWindow]);

  // meiva:note
  // One ResizeObserver per windowed session records each mounted row's height;
  // each commit observes only newly mounted rows and releases unmounted ones. Two
  // notifications can land before React commits, so the second builds on the
  // first through the ref rather than on the last committed map.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/bounded-window.meiva.okf.md
  useLayoutEffect(() => {
    if (!isWindowed || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      let next: Map<string, number> | null = null;
      for (const entry of entries) {
        const node = entry.target as HTMLElement;
        const key = node.dataset.turnRow;
        if (!key) continue;
        const height = entry.borderBoxSize[0]?.blockSize ?? node.getBoundingClientRect().height;
        const known = (next ?? rowHeightsRef.current).get(key);
        if (height <= 0 || (known !== undefined && Math.abs(known - height) < 1)) continue;
        next ??= new Map(rowHeightsRef.current);
        next.set(key, height);
      }
      if (!next) return;
      rowHeightsRef.current = next;
      setRowHeights(next);
    });
    rowObserverRef.current = { observer, nodes: new Set() };
    return () => {
      observer.disconnect();
      rowObserverRef.current = null;
    };
  }, [isWindowed]);

  useLayoutEffect(() => {
    const rows = rowObserverRef.current;
    const content = activityContentRef.current;
    if (!rows || !content) return;
    const mounted = new Set(
      [...content.children].filter(
        (node): node is HTMLElement => node instanceof HTMLElement && node.dataset.turnRow !== undefined,
      ),
    );
    for (const node of rows.nodes) {
      if (!mounted.has(node)) {
        rows.observer.unobserve(node);
        rows.nodes.delete(node);
      }
    }
    for (const node of mounted) {
      if (!rows.nodes.has(node)) {
        rows.observer.observe(node);
        rows.nodes.add(node);
      }
    }
  });

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
    if (isWindowed) {
      setInnerView((prev) =>
        prev.follow ? prev : { top: el.scrollTop, height: el.clientHeight, follow: true },
      );
    }
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [scrollToBottomSeq, liveWindow, expanded, isWindowed, onActivityAtBottomChange]);

  const syncInnerToBottom = useCallback(() => {
    const scroller = activityScrollRef.current;
    if (!scroller || !innerStickyRef.current) return;
    scroller.scrollTop = scroller.scrollHeight;
    lastInnerScrollTopRef.current = scroller.scrollTop;
  }, []);

  useLayoutEffect(() => {
    if (!liveWindow || !expanded) return;
    syncInnerToBottom();
  }, [liveWindow, expanded, children, windowedCount, syncInnerToBottom]);

  useLayoutEffect(() => {
    if (!liveWindow || !expanded) return;
    const content = activityContentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(syncInnerToBottom);
    ro.observe(content);
    return () => ro.disconnect();
  }, [liveWindow, expanded, syncInnerToBottom]);

  const liveDelta = turnActive && liveAnchor > 0 ? Math.max(liveNow - liveAnchor, 0) : 0;
  const totalWorkedMs = summary.workedMs + liveDelta;

  const hasWork = summary.thinking > 0 || summary.code > 0 || summary.output > 0;
  const hasLiveTimer = summary.lastLiveKind !== null && summary.lastLiveStartedAt > 0;
  const workedLabel = turnActive
    ? totalWorkedMs > 0 || hasWork || hasLiveTimer
      ? t("workingFor", { duration: formatDuration(totalWorkedMs) })
      : t("working")
    : hasWork && summary.workedMs > 0
      ? t("workedFor", { duration: formatDuration(summary.workedMs) })
      : t("workActivity");

  const actionSummary = expanded ? formatTurnSummary(summary, (key, values) =>
    t(key as Parameters<typeof t>[0], values),
  ) : "";

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
    const keys = Array.from({ length: windowedCount }, (_, index) => memberSources?.[index] ?? memberIds[index]);
    const sizes = keys.map((key) => rowHeights.get(key) ?? INNER_ESTIMATED_ROW_PX);
    const viewHeight = innerView.height > 0 ? innerView.height : INNER_UNMEASURED_VIEW_PX;
    const range = (() => {
      if (innerView.height <= 0 && innerView.top <= 0) {
        const start = Math.max(0, windowedCount - INNER_BOOTSTRAP_ROWS);
        return { start, end: windowedCount, before: span(sizes, 0, start, TIMELINE_TURN_ROW_GAP_PX), after: 0 };
      }
      const top = liveWindow && innerView.follow
        ? Math.max(0, span(sizes, 0, windowedCount, TIMELINE_TURN_ROW_GAP_PX) - viewHeight)
        : innerView.top;
      return windowForSizes(sizes, top, viewHeight, 0, TIMELINE_TURN_ROW_GAP_PX);
    })();
    return (
      <>
        {range.before > 0 ? (
          <div data-timeline-spacer="turn-before" style={{ height: range.before }} aria-hidden="true" />
        ) : null}
        {Array.from({ length: range.end - range.start }, (_, offset) => {
          const index = range.start + offset;
          return (
            <div key={keys[index]} data-turn-row={keys[index]}>
              {renderWindowedRow(index)}
            </div>
          );
        })}
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
        onClick={() => onToggle(toggleKey ?? id, expanded)}
        className={cn(
          "w-[calc(100%+0.5rem)] -ml-2 px-2 py-0.5 items-start gap-1.5 text-left select-none transition-colors",
          expanded
            ? "sticky z-20 bg-background rounded-md text-muted-foreground hover:text-foreground hover:bg-muted"
            : "rounded-lg text-muted-foreground/75 hover:text-foreground hover:bg-muted/50",
          FLEX,
          expanded && BAR_STICKY_TOP_CLASS,
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
        <span className={cn("gap-0.5", FLEX, FLEX_COL, MIN_W_0, FLEX_1)}>
          <span className={cn("items-center gap-1.5 text-[13px] leading-snug", FLEX, MIN_W_0)}>
            <Layers className="size-3.5 shrink-0 opacity-70" />
            <span className="truncate whitespace-nowrap tabular-nums">{workedLabel}</span>
          </span>
          {actionSummary && (
            <span className="pl-5 text-xs leading-snug">{actionSummary}</span>
          )}
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
          {expanded && !bounded ? (
            <div className="mt-1.5 ml-1.5 pl-2 pr-2 border-l border-border/60 space-y-1">
              {renderedBody}
            </div>
          ) : expanded ? (
            <ScrollArea
              data-turn-scroll="true"
              data-activity-scroll={liveWindow ? "true" : undefined}
              viewportTestId={liveWindow ? "activity-scroll-viewport" : "turn-scroll-viewport"}
              viewportRef={activityScrollRef}
              onScrollCapture={handleInnerScroll}
              className="mt-1.5"
              viewportClassName="[overflow-anchor:none] max-h-[var(--timeline-turn-max-h,50dvh)]"
            >
              <div ref={activityContentRef} className="ml-1.5 pl-2 pr-2 border-l border-border/60 space-y-1">
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
