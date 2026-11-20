"use client";

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import type { BackendTimelineItem, Quote } from "@/lib/contracts/types";
import { ScrollArea } from "@/components/ui/scroll-area";
import { findOldestTimelineCursor, isReattachedTimelineContext, parseItemIdParts, standingHeadNoteIds } from "@/lib/timeline/timeline";
import type { OlderLoadOrigin } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { BAR_CLEAR_TOP_PADDING_CLASS, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { ConnectionNotice } from "@/components/notifications/connection-notice";
import { QuoteNavigation, QuoteSelection, QuoteTarget } from "./view/quote/quote";
import { TimelineGroupList, useTimelineGroupWindow } from "./view/group-list";
import { CompactingBlock } from "./model/compacting-block";
import type { ReloadRestoreConfig } from "./model/reading-position";
import { useOlderHistoryPull } from "./model/use-older-history-pull";
import { useTimelineExpansion } from "./view/use-timeline-expansion";
import { useStickyThresholds } from "./model/sticky-thresholds";
import { READING_TOP_PX, readTurnAnchor, TAIL, type RowRef, type ViewAnchor } from "./anchor/dom";
import { rankOf } from "./anchor/rows";
import { usePlacement, type RevealDecision } from "./anchor/use-placement";
import { useViewportAnchor, type AnchorChangeCause } from "./anchor/use-viewport-anchor";
import { LoadOlderSpinner, LoadOlderKeyboardButton, ColdLoadSpinner, FOOTER_HEIGHT_VAR, ScrollToBottomButton } from "./overlays";

export type { ReloadRestoreConfig } from "./model/reading-position";
export type { RevealDecision } from "./anchor/use-placement";
export { hasSavedPlace as hasSavedTimelinePlace } from "./anchor/saved";

// meiva:note
// Cold fill: a conversation whose first page does not fill the viewport reads at most this many
// older pages in the background, so a short tail opens with history above it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
const COLD_FILL_MAX_PAGES = 2;

interface Props {
  onQuote?: (quote: Quote) => void;
  // meiva:note
  // What floats over the bottom of the timeline, just above the composer (the pending strip). It sits
  // in the blank space below the last row, so it appears without moving the conversation; only the
  // part taller than that space extends the log's bottom padding.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
  footer?: ReactNode;
  items: BackendTimelineItem[];
  compactBuffer?: CompactTransitionBuffer | null;
  threadKey?: string;
  scrollMemoryKey?: string;
  reloadRestore?: ReloadRestoreConfig;
  viewportObserverFactory?: (callback: ResizeObserverCallback) => ResizeObserver;

  streamingCode?: boolean;

  turnActive?: boolean;

  // meiva:note
  // Page-clock time of the thread's last received event; anchors the running turn's live clock
  // through the gaps where no block is in flight (0 = unknown).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  lastEventAt?: number;
  activeTurnId?: string | null;
  activeTurnStartedAt?: number;

  hasMoreOlder?: boolean;
  // meiva:note
  // An older page in flight is not a display state. `loadingOlder` is the
  // concurrency guard that the pull gesture, the keyboard control, the cold
  // first-viewport fill and the placement search all hold;
  // `readerLoadingOlder` says the reader asked for it, and only that presents
  // the loading strip above the messages.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
  loadingOlder?: boolean;
  readerLoadingOlder?: boolean;
  onLoadOlder?: (origin: OlderLoadOrigin) => void;
  onCancelBackgroundLoadOlder?: () => boolean;
  retainedItemsMax?: number;

  loading?: boolean;
  maxWidthCss?: string;
  // meiva:note
  // Where a first visit opens: the first delivery past the reader's seen position once that is
  // decided. A saved place always wins over it.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  reveal?: RevealDecision;
}

const DEFAULT_RELOAD_RESTORE: ReloadRestoreConfig = { state: "disabled" };
const DECIDED_NO_REVEAL: RevealDecision = { decided: true, itemId: null };

export const TimelineView = memo(function TimelineView({
  items: canonicalItems,
  compactBuffer = null,
  threadKey,
  scrollMemoryKey,
  reloadRestore = DEFAULT_RELOAD_RESTORE,
  viewportObserverFactory,
  streamingCode = false,
  turnActive = false,
  lastEventAt = 0,
  activeTurnId = null,
  activeTurnStartedAt = 0,
  hasMoreOlder = false,
  loadingOlder = false,
  readerLoadingOlder = false,
  onLoadOlder,
  onCancelBackgroundLoadOlder,
  retainedItemsMax = 250,
  loading = false,
  maxWidthCss,
  reveal = DECIDED_NO_REVEAL,
  onQuote,
  footer,
}: Props) {
  const t = useTranslations("timeline");
  const [quoteRequest, setQuoteRequest] = useState<{ quote: Quote; pages: number } | null>(null);
  const [quoteStatus, setQuoteStatus] = useState<string | null>(null);
  const [quoteTarget, setQuoteTarget] = useState<{ itemId: string; token: number } | null>(null);
  const bufferedItems = useMemo(
    () => compactBuffer?.rows.map((row) => row.item) ?? [],
    [compactBuffer],
  );
  const currentItems = useMemo(
    () => compactBuffer
      ? canonicalItems.filter((item) => (parseItemIdParts(item.item_id)?.rank ?? 0) === 0)
      : canonicalItems,
    [canonicalItems, compactBuffer],
  );
  const items = useMemo(
    () => compactBuffer ? [...bufferedItems, ...currentItems] : canonicalItems,
    [compactBuffer, bufferedItems, currentItems, canonicalItems],
  );

  // meiva:note
  // Load-bearing although this ref is never read: the `<div>` it is attached to
  // below is what the log's bottom padding applies above. Without that element
  // the last row overflows the log by exactly that padding and the gap above the
  // composer's rule silently falls to the composer's share alone (measured
  // 10px → 4px at 1440x900 with it removed). Delete it only together with a
  // replacement that keeps the padding.
  // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.md
  const endRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  // meiva:note
  // Extra scroll space below the content: it lets a toggled header stay where it was clicked when the
  // rows below are too short. Outside the content column, so it carries no row spacing.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
  const reserveRef = useRef<HTMLDivElement>(null);

  const { turnOverrides, toggleTurn, overrides, toggleExpanded } = useTimelineExpansion({ scrollMemoryKey, threadKey });
  const thresholds = useStickyThresholds();

  const handlers = useRef<{
    onChange: (anchor: ViewAnchor, cause: AnchorChangeCause) => void;
    onUnresolved: (row: RowRef) => void;
    onFree: () => void;
    onRideEnd: () => void;
  }>({ onChange: () => undefined, onUnresolved: () => undefined, onFree: () => undefined, onRideEnd: () => undefined });
  const onAnchorChange = useCallback((anchor: ViewAnchor, cause: AnchorChangeCause) => handlers.current.onChange(anchor, cause), []);
  const onUnresolved = useCallback((row: RowRef) => handlers.current.onUnresolved(row), []);
  const onFree = useCallback(() => handlers.current.onFree(), []);
  const onRideEnd = useCallback(() => handlers.current.onRideEnd(), []);
  const pinnedHeadIds = useMemo(() => {
    const notes = standingHeadNoteIds(items);
    return new Set(items.filter((item) => isReattachedTimelineContext(item) || notes.has(item.item_id)).map((item) => item.item_id));
  }, [items]);
  const pinnedHeadRef = useRef(pinnedHeadIds);
  useLayoutEffect(() => { pinnedHeadRef.current = pinnedHeadIds; });
  const pinnedHead = useCallback((id: string) => pinnedHeadRef.current.has(id), []);
  const { controls, atEnd } = useViewportAnchor({
    wrapperRef, viewportRef, contentRef, reserveRef, thresholds,
    onChange: onAnchorChange, onUnresolved, pinnedHead, onFree, onRideEnd, viewportObserverFactory,
  });

  // meiva:note
  // The footer's height drives the log's bottom padding and the arrow's offset through one CSS
  // variable. The writer applies the anchor in the same callback: left to its own layout observer,
  // the padding change would reach it one observation round later, after a frame with the grown
  // strip over the last row was already laid out.
  const footerRef = useRef<HTMLDivElement>(null);
  const hasFooter = footer !== undefined;
  useLayoutEffect(() => {
    const wrapper = wrapperRef.current;
    const node = footerRef.current;
    if (!wrapper || !node) return;
    const sync = () => {
      const next = `${node.offsetHeight}px`;
      if (wrapper.style.getPropertyValue(FOOTER_HEIGHT_VAR) === next) return;
      wrapper.style.setProperty(FOOTER_HEIGHT_VAR, next);
      controls.apply();
    };
    sync();
    if (typeof ResizeObserver === "undefined") return () => wrapper.style.removeProperty(FOOTER_HEIGHT_VAR);
    const observer = new ResizeObserver(sync);
    observer.observe(node);
    return () => {
      observer.disconnect();
      wrapper.style.removeProperty(FOOTER_HEIGHT_VAR);
    };
  }, [controls, hasFooter]);

  // meiva:note
  // A first page that does not fill the viewport reads up to COLD_FILL_MAX_PAGES older pages before a
  // first visit opens, so history never lands above rows the reader is already looking at.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
  const coldFill = useRef<{ pages: number; open: boolean; requested: string | null; sawLoading: boolean }>({
    pages: 0, open: true, requested: null, sawLoading: false,
  });
  const filling = useCallback(() => {
    const fill = coldFill.current;
    const viewport = controls.viewport();
    if (!fill.open || loading || !viewport || viewport.clientHeight <= 0 || !hasMoreOlder || !onLoadOlder) return false;
    if (viewport.scrollHeight > viewport.clientHeight) return false;
    const inFlight = loadingOlder || (fill.requested !== null && fill.requested === (findOldestTimelineCursor(canonicalItems)?.item_id ?? null));
    return fill.pages < COLD_FILL_MAX_PAGES || inFlight;
  }, [canonicalItems, controls, hasMoreOlder, loading, loadingOlder, onLoadOlder]);
  const placement = usePlacement({
    threadKey,
    entryKey: scrollMemoryKey,
    reloadRestore,
    items: canonicalItems,
    compactBuffer,
    loading,
    hasMoreOlder,
    loadingOlder,
    onLoadOlder,
    onCancelBackgroundLoadOlder,
    reveal,
    anchor: controls,
    filling,
  });
  // meiva:note
  // Only a following reader releases the oldest rows (the retention rule in the paging doc): the
  // tail stays where it is while rows leave above it. Not during the arrow's ride, whose end would
  // move under it; the ride's arrival trims.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
  const trimWhileFollowing = useCallback(() => {
    if (controls.current()?.kind !== "tail" || controls.riding()) return;
    const store = useTimelineStore.getState();
    if (store.activeThreadId === null || String(store.activeThreadId) !== threadKey) return;
    if (store.compactBuffer || store.items.length <= retainedItemsMax) return;
    store.trimOldestWhileFollowing(retainedItemsMax);
  }, [controls, retainedItemsMax, threadKey]);
  handlers.current = { onChange: placement.onAnchorChange, onUnresolved: placement.onUnresolved, onFree: placement.onFree, onRideEnd: trimWhileFollowing };
  const visible = placement.visible;
  // meiva:note
  // The writer applies after placement has seen this commit's rows and before the window measures it.
  useLayoutEffect(() => {
    controls.apply();
  });

  const isFollowing = useCallback(() => controls.current()?.kind === "tail", [controls]);
  const { groups, groupEntries, virtualEnabled, virtualRange, rowRange } = useTimelineGroupWindow({
    canonicalItems,
    compactBuffer,
    bufferedItems,
    currentItems,
    threadKey,
    turnOverrides,
    turnActive,
    viewportRef,
    contentRef,
    isFollowing,
    pin: placement.pin,
  });

  useLayoutEffect(() => {
    if (visible) trimWhileFollowing();
  });

  // meiva:note
  // History paging: the reader's pull or keyboard control, and a cold fill for a short first page.
  const canPullOlder = useCallback(
    () => visible && hasMoreOlder && !loadingOlder && !!onLoadOlder,
    [hasMoreOlder, loadingOlder, onLoadOlder, visible],
  );
  const pullOlderPage = useCallback(() => {
    if (!canPullOlder()) return false;
    onLoadOlder?.("reader");
    return true;
  }, [canPullOlder, onLoadOlder]);
  const onUpwardIntent = useCallback(() => {
    coldFill.current.open = false;
    controls.releaseTail();
  }, [controls]);
  const pull = useOlderHistoryPull(threadKey, canPullOlder, pullOlderPage, onUpwardIntent);
  useEffect(() => {
    const viewport = controls.viewport();
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => pull.onWheel(event, viewport);
    const onTouchStart = (event: TouchEvent) => pull.onTouchStart(event, viewport);
    const onTouchMove = (event: TouchEvent) => pull.onTouchMove(event);
    viewport.addEventListener("wheel", onWheel, { passive: false });
    viewport.addEventListener("touchstart", onTouchStart, { passive: true });
    viewport.addEventListener("touchmove", onTouchMove, { passive: false });
    viewport.addEventListener("touchend", pull.onTouchEnd, { passive: true });
    viewport.addEventListener("touchcancel", pull.onTouchCancel, { passive: true });
    return () => {
      viewport.removeEventListener("wheel", onWheel);
      viewport.removeEventListener("touchstart", onTouchStart);
      viewport.removeEventListener("touchmove", onTouchMove);
      viewport.removeEventListener("touchend", pull.onTouchEnd);
      viewport.removeEventListener("touchcancel", pull.onTouchCancel);
    };
  }, [controls, pull]);
  useLayoutEffect(() => {
    const fill = coldFill.current;
    const viewport = controls.viewport();
    if (!fill.open || !placement.fillAllowed || loading || !viewport || viewport.clientHeight <= 0 || canonicalItems.length === 0) return;
    if (loadingOlder) {
      if (fill.requested !== null) fill.sawLoading = true;
      return;
    }
    const oldest = findOldestTimelineCursor(canonicalItems)?.item_id ?? null;
    // meiva:note
    // One page at a time: the next waits until the last one has landed (the oldest row moved); a
    // page that ended without reaching further back ends the fill.
    if (fill.requested !== null && fill.requested === oldest) {
      if (fill.sawLoading) fill.open = false;
      return;
    }
    fill.sawLoading = false;
    if (viewport.scrollHeight > viewport.clientHeight || fill.pages >= COLD_FILL_MAX_PAGES || !hasMoreOlder || !onLoadOlder) {
      fill.open = false;
      return;
    }
    fill.pages += 1;
    fill.requested = oldest;
    onLoadOlder("background");
  });

  // meiva:note
  // Manual expansion keeps the clicked header where it is and releases tail following, including
  // for active work (user ruling 2026-11-17). Collapsing near the end may need reserve space to
  // keep the header in place; the reader spends it by scrolling up past the natural end.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/live-block.md
  const handleToggleTurn = useCallback(
    (id: string, currentlyExpanded: boolean, button: HTMLButtonElement) => {
      const viewport = controls.viewport();
      const header = viewport ? readTurnAnchor(viewport, button) : null;
      if (header) controls.place(header, { hold: true });
      toggleTurn(id, currentlyExpanded);
    },
    [controls, toggleTurn],
  );

  // meiva:note
  // The arrow rides to the tail and follows it. Inside a toggle's reserve it uses the remaining range.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/send-anchor.md
  const handleScrollToBottom = useCallback(() => {
    const viewport = controls.viewport();
    if (!viewport) return;
    const end = viewport.scrollHeight - viewport.clientHeight;
    if (end - viewport.scrollTop <= 1) return;
    const natural = end - controls.reserveHeight();
    if (viewport.scrollTop > natural + 0.5) {
      controls.placeScrollTop(end);
      return;
    }
    controls.place(TAIL, { smooth: true });
  }, [controls]);

  const navigateQuote = useCallback((quote: Quote) => {
    if (!quote.source_message_id && quote.source_inbound_id == null) {
      setQuoteStatus(t("quoteUnavailable"));
      return;
    }
    setQuoteStatus(null);
    setQuoteRequest({ quote, pages: 0 });
  }, [t]);
  useEffect(() => {
    if (!quoteRequest) return;
    const { quote, pages } = quoteRequest;
    const item = canonicalItems.find((candidate) =>
      quote.source_agent_id === Number(threadKey) &&
      (quote.source_message_id ? candidate.source_message_id === quote.source_message_id :
        quote.source_inbound_id != null && (candidate.source_inbound_id ?? candidate.inbound_id) === quote.source_inbound_id) &&
      (quote.source_block_idx == null || candidate.source_block_idx === quote.source_block_idx));
    if (item) {
      const entry = groupEntries.find(({ group }) => group.kind === "single" ? group.member.item === item : group.members.some((member) => member.item === item));
      if (entry && !entry.runExpanded) toggleTurn(entry.groupKey, false);
      toggleExpanded(item.item_id, false);
      setQuoteTarget((previous) => ({ itemId: item.item_id, token: (previous?.token ?? 0) + 1 }));
      placement.request({ kind: "row", row: { itemId: item.item_id, rank: rankOf(item, compactBuffer) }, offset: READING_TOP_PX });
      setQuoteRequest(null);
    } else if (!loadingOlder && hasMoreOlder && onLoadOlder && pages < 3) {
      setQuoteRequest({ quote, pages: pages + 1 });
      onLoadOlder("reader");
    } else if (!loadingOlder) {
      setQuoteStatus(t("quoteUnavailable"));
      setQuoteRequest(null);
    }
  }, [quoteRequest, canonicalItems, compactBuffer, threadKey, groupEntries, placement, toggleTurn, toggleExpanded, loadingOlder, hasMoreOlder, onLoadOlder, t]);

  const [announcedTurnActive, setAnnouncedTurnActive] = useState(turnActive);
  const [turnAnnouncement, setTurnAnnouncement] = useState<string | null>(null);
  if (announcedTurnActive !== turnActive) {
    setAnnouncedTurnActive(turnActive);
    setTurnAnnouncement(turnActive ? t("agentResponding") : t("agentResponseComplete"));
  }

  const showRing = loading || !visible;
  return (
    <QuoteNavigation.Provider value={navigateQuote}>
    <QuoteTarget.Provider value={quoteTarget}>
    <div ref={wrapperRef} className={cn("relative", FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
      {onQuote ? <QuoteSelection scope={wrapperRef} items={items} agentId={Number(threadKey)} onQuote={onQuote} /> : null}
      {quoteStatus ? <div role="status" className="absolute bottom-12 left-4 z-40 rounded border border-border bg-background px-3 py-2 text-sm">{quoteStatus}</div> : null}
      <LoadOlderSpinner loadingOlder={readerLoadingOlder && !showRing} />
      {hasMoreOlder && onLoadOlder ? <LoadOlderKeyboardButton loading={loadingOlder} onClick={pullOlderPage} /> : null}
      <ColdLoadSpinner show={showRing} />
      <ConnectionNotice />
      <ScrollArea
        className="h-full text-[13px] leading-relaxed"
        viewportRef={viewportRef}
        viewportClassName="[overflow-anchor:none] overscroll-y-contain"
        scrollbarActivity="input"
      >
        <div
          // meiva:note
          // Rows are hidden (not unmounted) until their place is resolved: the first painted frame is
          // the destination.
          // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
          className="w-full"
          style={visible ? undefined : { visibility: "hidden" }}
          data-timeline-column
        >
          <div
            ref={contentRef}
            role="log"
            data-timeline-item-count={items.length}
            aria-live="polite"
            aria-relevant="additions"
            aria-busy={!visible}
            style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
            // meiva:note
            // `timeline-log` holds the bottom padding: the breathing room between the last message /
            // Live Activity Window and the composer's top rule, which the footer may use; endRef below
            // keeps the padding inside the log.
            // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.md
            className={cn("timeline-log mx-auto w-full px-2 space-y-3 sm:px-4", BAR_CLEAR_TOP_PADDING_CLASS, virtualEnabled && "timeline-virtual")}
          >
            {turnAnnouncement ? (
              <span key={turnAnnouncement} className="sr-only" data-testid="timeline-turn-announcement">
                {turnAnnouncement}
              </span>
            ) : null}
            <TimelineGroupList
              items={items}
              compactBuffer={compactBuffer}
              streamingCode={streamingCode}
              turnActive={turnActive}
              loading={showRing || readerLoadingOlder}
              lastEventAt={lastEventAt}
              activeTurnId={activeTurnId}
              activeTurnStartedAt={activeTurnStartedAt}
              overrides={overrides}
              toggleExpanded={toggleExpanded}
              toggleTurn={handleToggleTurn}
              groups={groups}
              groupEntries={groupEntries}
              virtualEnabled={virtualEnabled}
              virtualRange={virtualRange}
              rowRange={rowRange}
            />
            <CompactingBlock />
            <div ref={endRef} />
          </div>
          <div ref={reserveRef} data-timeline-reserve aria-hidden="true" />
        </div>
      </ScrollArea>
      {footer === undefined ? null : (
        <div ref={footerRef} data-timeline-footer className="pointer-events-none absolute inset-x-0 bottom-0 z-20">
          {footer}
        </div>
      )}
      <ScrollToBottomButton atBottom={atEnd || !visible} onClick={handleScrollToBottom} />
    </div>
    </QuoteTarget.Provider>
    </QuoteNavigation.Provider>
  );
});
