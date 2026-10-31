"use client";

// meiva:note
// TurnBlock renders secondary agent items in two modes: a historical collapsed
// single-line inline disclosure (whose header pins at top-11 below HeaderBar
// while expanded, with child CardHeaders pinning one level deeper via
// --turn-header-h), and a fixed-height (h-56) Live Activity Window while
// liveWindow is true on the active trailing turn, using the shared auto-hiding
// ScrollArea scrollbar and feeding its detached scroll state into the outer
// timeline's ScrollToBottomButton.
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
} from "react";

import { ScrollArea } from "@/components/ui/scroll-area";
import { formatDuration, type SdkCall } from "@/lib/format/item-summary";
import { cn } from "@/lib/format/utils";

import { STUCK_HEADER_CLS, UNSTUCK_HEADER_CLS } from "./card";
import { CallBadge, STICKY_HEADER_CLS } from "./view/card-summary";
import { useNow } from "./model/reasoning-clock";
import { formatTurnSummary, turnSummaryEqual, type TurnSummary } from "./model/runs";
import {
  BAR_HEIGHT_PX,
  FLEX,
  FLEX_1,
  FLEX_COL,
  MIN_H_0,
  MIN_W_0,
  OVERFLOW_CLIP,
  OVERFLOW_HIDDEN,
} from "@/lib/layout/layout";

/**
 * Identify which pinned headers are currently stuck at the top of the
 * viewport — at most one per level. Level 1 shares one sticky line: a work
 * block's header (`data-turn-expanded="true"` on the turn element) or a
 * top-level message card's header (`data-card-sticky="true"` on the row).
 * Level 2 is the pinned child card inside an expanded work block
 * (`data-turn-child="true"` on the row): its header sticks just below its own
 * block's header, so its line is that block header's rect bottom — the
 * in-flow position while the block is unpinned (children cannot reach it
 * before the block's header does) and the pinned bottom once it sticks.
 *
 * Sibling blocks never overlap in flow, so at most one candidate per level is
 * stuck at a time; when multiple cross the line (a mid-scroll intermediate
 * state), each level returns the latest/closest one that crossed the viewport
 * top (highest top position that is <= that level's line).
 */
export interface StuckHeaderIds {
  /** The level-1 pinned header: an expanded work block's or a top-level card's. */
  topId: string | null;
  /** The child card header pinned nested under that work block's header, when
   *  one has crossed its line; null when no block is pinned or none reached it. */
  childId: string | null;
}

export function findClosestStuckHeaderId(
  viewport: HTMLElement,
  topOffset: number = BAR_HEIGHT_PX,
): StuckHeaderIds {
  const vpRect = viewport.getBoundingClientRect();
  const stickyLine = vpRect.top + topOffset;
  // A candidate is stuck when its block's top has reached or passed its line,
  // and its bottom has not completely scrolled past it — a bare `> line`, no
  // exit buffer: while ANY part of the block sits below the line, its pinned
  // header is mid push-out and still masks that tail. Flipping to the
  // transparent variant earlier (the former +20px buffer, under the ~30px
  // header) unmasked the block's last text lines while the detaching header
  // still covered them — two texts in one band (user report 2026-09-19).
  // The +1px top tolerance relies on the ≥2px in-flow gap below a block header
  // (the header's pt-0.5): if that gap disappeared, the first child of an
  // unpinned block — whose line is the header rect's bottom — would read as
  // crossed and get the nested sticky styling early.
  const crossed = (r: DOMRect, line: number) => r.top <= line + 1 && r.bottom > line;

  let topId: string | null = null;
  let topTop = -Infinity;
  let topOwner: HTMLElement | null = null;
  // Child rows are excluded here: their block's header is the level-1 marker,
  // and they surface on the level-2 pass below.
  for (const el of viewport.querySelectorAll<HTMLElement>(
    '[data-turn-expanded="true"], [data-card-sticky="true"]:not([data-turn-child="true"])',
  )) {
    const id = el.getAttribute("data-turn-id") ?? el.getAttribute("data-item-id");
    if (!id) continue;
    const r = el.getBoundingClientRect();
    if (crossed(r, stickyLine) && r.top >= topTop) {
      topTop = r.top;
      topId = id;
      topOwner = el;
    }
  }

  // A nested header belongs only to the selected top-level turn. Measuring
  // every historical child's geometry defeats offscreen layout containment;
  // an unpinned turn or a top-level message card has no nested sticky owner.
  const header = topOwner?.matches('[data-turn-expanded="true"]')
    ? topOwner.querySelector<HTMLElement>('[data-testid="turn-toggle"]')
    : null;
  if (!topOwner || !header) return { topId, childId: null };
  const childLine = header.getBoundingClientRect().bottom;

  let childId: string | null = null;
  let childTop = -Infinity;
  for (const el of topOwner.querySelectorAll<HTMLElement>(
    '[data-turn-child="true"][data-card-sticky="true"]',
  )) {
    const id = el.getAttribute("data-item-id");
    if (!id) continue;
    // The child line sits under the block's header — see the doc comment.
    const r = el.getBoundingClientRect();
    if (crossed(r, childLine) && r.top >= childTop) {
      childTop = r.top;
      childId = id;
    }
  }

  return { topId, childId };
}

interface TurnBlockProps {
  // The turn's first member item_id, stamped as data-item-id so the load-older
  // scroll anchor has a stable node even when the topmost content is a collapsed
  // turn (whose inner rows — and their own data-item-id — are not mounted).
  id: string;
  // Every member's item_id, space-joined onto data-turn-member-ids. A turn's
  // FIRST member changes across a load-older prepend whenever the fetched
  // older window extends the turn's front (a new older secondary item joins
  // ahead of the current first) — data-item-id alone then no longer matches
  // an anchor captured before the prepend, even though that item is still
  // right there, just no longer first. The scroll-anchor lookup in
  // TimelineView matches on this attribute too so it finds the turn
  // regardless of which member id was captured.
  memberIds: readonly string[];
  summary: TurnSummary;
  expanded: boolean;
  // Called with this block's id and its current rendered state, so the parent
  // can pass one stable callback to every block (an inline per-block closure
  // would defeat the memo below).
  onToggle: (id: string, currentlyExpanded: boolean) => void;
  // Whether the agent is mid-turn — drives the live "working for X" / "worked for X" clock.
  turnActive?: boolean;
  liveWindow?: boolean;
  onActivityAtBottomChange?: (atBottom: boolean) => void;
  scrollToBottomSeq?: number;
  // Whether this turn block is currently actively stuck at the top of the viewport.
  isStuck?: boolean;
  // The inner rows — passed only when expanded (null when collapsed).
  children?: ReactNode;
  timelineSource?: "buffer" | "canonical";
  displayRank?: number;
}

function sameIds(a: readonly string[], b: readonly string[]): boolean {
  return a === b || (a.length === b.length && a.every((id, index) => id === b[index]));
}

// Every streamed chunk regroups the timeline, rebuilding each turn's
// memberIds array and summary object even when the turn did not change.
// Compare those two by value so a historical (collapsed, childless) block
// skips its render; every other prop is compared by identity. An expanded
// block receives fresh children elements and re-renders as before.
function turnBlockPropsEqual(prev: TurnBlockProps, next: TurnBlockProps): boolean {
  return (
    prev.id === next.id &&
    prev.expanded === next.expanded &&
    prev.onToggle === next.onToggle &&
    prev.turnActive === next.turnActive &&
    prev.liveWindow === next.liveWindow &&
    prev.onActivityAtBottomChange === next.onActivityAtBottomChange &&
    prev.scrollToBottomSeq === next.scrollToBottomSeq &&
    prev.isStuck === next.isStuck &&
    prev.children === next.children &&
    prev.timelineSource === next.timelineSource &&
    prev.displayRank === next.displayRank &&
    sameIds(prev.memberIds, next.memberIds) &&
    turnSummaryEqual(prev.summary, next.summary)
  );
}

export const TurnBlock = memo(function TurnBlock({
  id,
  memberIds,
  summary,
  expanded,
  onToggle,
  turnActive,
  liveWindow = false,
  onActivityAtBottomChange,
  scrollToBottomSeq = 0,
  isStuck,
  children,
  timelineSource = "canonical",
  displayRank,
}: TurnBlockProps) {
  const t = useTranslations("timeline");
  // Live clock: while a block of this turn is streaming, re-render on the
  // shared live-clock tick so the displayed elapsed time advances in real
  // time. The streaming block is the ONLY thing that moves the display —
  // every other input is a committed number that changes when a new summary
  // arrives — so an active turn sitting between blocks does not re-render 10x
  // a second.
  const liveBlockStartedAt =
    turnActive && summary.lastLiveKind != null && summary.lastLiveStartedAt > 0
      ? summary.lastLiveStartedAt
      : 0;
  const liveNow = useNow(liveBlockStartedAt > 0);

  // meiva:note
  // Nested pin line for this block's child headers (task #3215): a child
  // header sticks just below this block's header, so --turn-header-h must
  // track its real border-box height synchronously before first paint and
  // across runtime wraps in historical expanded mode (the Live Activity Window
  // scrolls inside its own fixed-height frame).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
  const rootRef = useRef<HTMLDivElement | null>(null);
  const headerRef = useRef<HTMLButtonElement | null>(null);
  useLayoutEffect(() => {
    if (!expanded || liveWindow) return;
    const root = rootRef.current;
    const header = headerRef.current;
    if (!root || !header || typeof ResizeObserver === "undefined") return;
    const measure = () =>
      root.style.setProperty("--turn-header-h", `${header.getBoundingClientRect().height}px`);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(header, { box: "border-box" });
    return () => observer.disconnect();
  }, [expanded, liveWindow]);

  // meiva:note
  // Inner sticky-bottom auto-follow for the fixed-height Live Activity Window;
  // its detached state feeds the outer timeline's single ScrollToBottomButton.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
  const activityScrollRef = useRef<HTMLDivElement | null>(null);
  const activityContentRef = useRef<HTMLDivElement | null>(null);
  const innerStickyRef = useRef(true);
  const lastInnerScrollTopRef = useRef(0);
  const seenScrollSeqRef = useRef(scrollToBottomSeq);

  const handleInnerScroll = useCallback(
    (event: ReactUIEvent<HTMLDivElement>) => {
      const el = activityScrollRef.current;
      if (!el) return;
      if (event.target !== el && event.target !== event.currentTarget) return;
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
    [onActivityAtBottomChange],
  );

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
  }, [liveWindow, expanded, children]);

  // How long the block that is streaming right now has been running. Zero when
  // the turn is idle or its last item is already committed. One value, read by
  // both the header clock and the sub-block timing line below, so the two
  // cannot disagree about the block in flight.
  const liveDelta = liveBlockStartedAt > 0 ? Math.max(liveNow - liveBlockStartedAt, 0) : 0;
  const totalWorkedMs = summary.workedMs + liveDelta;

  // The header timer, two states over ONE basis — the sum of the turn's block
  // durations. A turn is a maximal run of secondary items, so it can span a
  // restart marker or a wake-up and the idle gap before the agent picked the
  // work back up; wall-clock across the turn charges that gap as work.
  // - LIVE (turnActive): "Working for Xs" = the committed workedMs plus the
  //   in-flight block's elapsed. Unconditional — it does not wait for a work
  //   item to land, so the clock is present from the first moment of the turn
  //   (reading zero while the LLM is still silent after a system wake-up).
  // - DONE: "Worked for Xs" = workedMs alone. The block that was in flight has
  //   committed its duration into it, so the number does not jump at the
  //   handover. Shown only when the turn contains actual agent work — a turn of
  //   only system notes shows its counts ("1 system note") with no timer (the
  //   notes are instantaneous inserts, not work).
  const hasWork = summary.thinking > 0 || summary.code > 0 || summary.output > 0;
  const workedLabel = turnActive
    ? t("workingFor", { duration: formatDuration(totalWorkedMs) })
    : hasWork && summary.workedMs > 0
      ? t("workedFor", { duration: formatDuration(summary.workedMs) })
      : null;

  const actionSummary = formatTurnSummary(summary, (key, values) =>
    t(key as Parameters<typeof t>[0], values),
  );

  // Build the header line: "Worked for Xs · 5 turns · 2 agent messages".
  // actionSummary covers every member kind except the deliberately-unlabeled
  // system prompt (task #3557) — a turn of only prompts renders a blank label.
  const headerParts: string[] = [];
  if (workedLabel) headerParts.push(workedLabel);
  if (actionSummary) headerParts.push(actionSummary);

  const hasSdkCalls = summary.sdkCalls.length > 0;

  if (liveWindow) {
    return (
      <div
        ref={rootRef}
        data-item-id={id}
        data-timeline-source={timelineSource}
        data-display-rank={displayRank}
        data-turn-id={id}
        data-turn-expanded="false"
        data-turn-member-ids={memberIds.join(" ")}
        data-live-activity-window="true"
        aria-live="off"
        className={cn(
          "rounded-xl border border-border/80 bg-card/90 shadow-xs",
          "dark:bg-[#252421] dark:border-white/[0.08]",
          OVERFLOW_HIDDEN,
        )}
      >
        <button
          ref={headerRef}
          type="button"
          onClick={() => onToggle(id, expanded)}
          aria-expanded={expanded}
          data-testid="turn-toggle"
          data-expanded={expanded}
          data-stuck="false"
          className={cn(
            "w-full h-9 px-3.5 items-center justify-between gap-3 text-left select-none transition-colors",
            "bg-muted/35 hover:bg-muted/55 dark:bg-white/[0.025] dark:hover:bg-white/[0.045]",
            expanded && "border-b border-border/55 dark:border-white/[0.06]",
            FLEX,
          )}
        >
          <span className={cn("items-center gap-2", FLEX, MIN_W_0, FLEX_1)}>
            <Layers className="size-3.5 shrink-0 text-muted-foreground/75" />
            <span className="truncate text-[13px] font-medium text-foreground/88">
              {t("working")}
            </span>
            {actionSummary ? (
              <span className="hidden sm:inline truncate text-[12px] font-mono text-muted-foreground/75 tabular-nums">
                {`· ${actionSummary}`}
              </span>
            ) : null}
          </span>
          <span className={cn("items-center gap-2 shrink-0", FLEX)}>
            {totalWorkedMs > 0 ? (
              <span className="text-[12px] font-mono tabular-nums text-muted-foreground/85">
                {formatDuration(totalWorkedMs)}
              </span>
            ) : null}
            <ChevronRight
              className={cn(
                "size-3.5 text-muted-foreground/70 transition-transform duration-200 motion-reduce:transition-none",
                expanded && "rotate-90",
              )}
            />
          </span>
        </button>

        <div
          className={cn(
            "grid transition-[grid-template-rows] duration-200 ease-out motion-reduce:transition-none",
            expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
          )}
        >
          <div className={cn("relative", OVERFLOW_HIDDEN, MIN_H_0, MIN_W_0)}>
            <ScrollArea
              data-activity-scroll="true"
              viewportTestId="activity-scroll-viewport"
              viewportRef={activityScrollRef}
              onScrollCapture={handleInnerScroll}
              className="h-56"
              viewportClassName="[overflow-anchor:none] overscroll-y-contain px-2.5 py-2"
            >
              <div ref={activityContentRef} className="space-y-1">
                {children}
              </div>
            </ScrollArea>
          </div>
        </div>
      </div>
    );
  }

  // meiva:note
  // The collapsed strip is one fixed-height line, so its chips are expanded-only:
  // a collapsed block carrying two to four chips stood 50px tall (68px when its
  // summary also wrapped) while a chip-free one stood 30px, and those strips sit
  // between message cards, so the gap between two cards varied by up to 38px for
  // no reason the reader can see (user report 2026-10-21: unify toward the narrow
  // one).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
  const sdkChips = expanded && hasSdkCalls ? (
    <span className={cn("flex-wrap items-center gap-x-1.5 gap-y-0.5 pl-5 text-[11px] font-mono opacity-75", FLEX)}>
      {summary.sdkCalls.map((c, i) => (
        <TurnCallChip key={c.method} call={c} showDot={i > 0} />
      ))}
    </span>
  ) : null;

  return (
    <div
      ref={rootRef}
      data-item-id={id}
      data-timeline-source={timelineSource}
      data-display-rank={displayRank}
      data-turn-id={id}
      data-turn-expanded={expanded}
      data-turn-member-ids={memberIds.join(" ")}
      aria-live="off"
      className="relative"
    >
      <button
        ref={headerRef}
        type="button"
        onClick={() => onToggle(id, expanded)}
        className={cn(
          "items-start gap-1.5 text-left select-none transition-colors",
          expanded
            ? cn(
                "px-2 py-0.5 rounded-md text-muted-foreground hover:text-foreground",
                isStuck ? "w-full" : "w-[calc(100%+0.5rem)] -ml-2",
              )
            : "max-w-full rounded-lg px-2 py-0.5 -ml-2 text-muted-foreground/75 hover:text-foreground hover:bg-muted/50",
          // STICKY_HEADER_CLS pins below the floating HeaderBar (top-11 =
          // BAR_HEIGHT_PX / BAR_HEIGHT_CLASS in @/lib/layout); the stuck variant
          // masks the detail rows scrolling beneath it. Shared with CardHeader.
          expanded && STICKY_HEADER_CLS,
          expanded && (isStuck ? STUCK_HEADER_CLS : UNSTUCK_HEADER_CLS),
          FLEX,
        )}
        aria-expanded={expanded}
        data-testid="turn-toggle"
        data-expanded={expanded}
        data-stuck={expanded && Boolean(isStuck) ? "true" : "false"}
      >
        <ChevronRight
          className={cn(
            "size-3.5 shrink-0 opacity-60 mt-0.5 transition-transform duration-200 motion-reduce:transition-none",
            expanded && "rotate-90",
          )}
        />
        <span className={cn("gap-0.5", FLEX, FLEX_COL, MIN_W_0, expanded && FLEX_1)}>
          {/* Summary line: "Worked for Xs · 5 turns · 2 agent messages".
              tabular-nums: the live clock rewrites the digits ~10x/s; fixed-
              width numerals keep the line from wobbling as digits change. */}
          <span className={cn("items-center gap-1.5 text-[13px] leading-snug", FLEX, MIN_W_0)}>
            <Layers className="size-3.5 shrink-0 opacity-70" />
            <span className="truncate whitespace-nowrap tabular-nums">{headerParts.join(" · ") || ""}</span>
          </span>
          {sdkChips}
        </span>
      </button>
      {/* The collapse wrapper must clip (overflow-clip, not overflow-hidden):
          a scroll container here would become the child headers' scrollport
          and break their nested sticky (task #3215). min-w-0 keeps the auto
          grid column from blowing out to the item's min-content width when a
          run holds wide content — the timeline otherwise overflows sideways
          (#3305, user report). */}
      <div
        className={cn(
          "grid transition-[grid-template-rows] duration-200 ease-out motion-reduce:transition-none",
          expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
      >
        <div className={cn(OVERFLOW_CLIP, MIN_H_0, MIN_W_0)}>
          <div className="mt-1.5 ml-1.5 pl-3 border-l border-border/60 space-y-1">{children}</div>
        </div>
      </div>
    </div>
  );
}, turnBlockPropsEqual);

// meiva:note
// Immediate fixed-height activity shell rendered right after user message
// submission before the first reasoning or tool item arrives, preventing layout jump.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
export const PendingLiveActivityWindow = memo(function PendingLiveActivityWindow() {
  const t = useTranslations("timeline");
  return (
    <div
      data-live-activity-window="true"
      data-testid="pending-live-activity-window"
      aria-live="off"
      className={cn(
        "rounded-xl border border-border/80 bg-card/90 shadow-xs",
        "dark:bg-[#252421] dark:border-white/[0.08]",
        OVERFLOW_HIDDEN,
      )}
    >
      <div
        className={cn(
          "w-full h-9 px-3.5 items-center justify-between gap-3 select-none",
          "bg-muted/35 border-b border-border/55 dark:bg-white/[0.025] dark:border-white/[0.06]",
          FLEX,
        )}
      >
        <span className={cn("items-center gap-2", FLEX, MIN_W_0)}>
          <Layers className="size-3.5 shrink-0 text-muted-foreground/75" />
          <span className="truncate text-[13px] font-medium text-foreground/88">
            {t("working")}
          </span>
        </span>
      </div>
      <ScrollArea
        data-activity-scroll="true"
        className="h-56"
        viewportClassName="overscroll-y-contain px-3.5 py-3"
      >
        <div className="space-y-2.5 pt-0.5">
          <div className="h-3 w-44 rounded bg-muted/70 animate-pulse motion-reduce:animate-none" />
          <div className="h-3 w-72 max-w-full rounded bg-muted/45 animate-pulse motion-reduce:animate-none" />
        </div>
      </ScrollArea>
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
