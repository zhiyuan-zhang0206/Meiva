"use client";

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ScrollArea } from "@/components/ui/scroll-area";
import { parseItemIdParts } from "@/lib/timeline/timeline";
import type { OlderLoadOrigin } from "@/lib/timeline/use-timeline";
import {
  type StickyController,
  createStickyController,
  isAtBottom,
} from "@/lib/layout/sticky";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { readScrollMemory, type SavedScroll } from "@/lib/layout/scroll-memory";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { BAR_CLEAR_TOP_PADDING_CLASS, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { ConnectionNotice } from "@/components/notifications/connection-notice";
import { TimelineGroupList, useTimelineGroupWindow } from "./view/group-list";
import { CompactingBlock } from "./model/compacting-block";
import { applyBookmarkRestore, settleRestoredPosition as settleRestoredPositionImpl, type BookmarkState } from "./model/bookmark-restore";
import { rememberTimelinePosition } from "./model/reading-position";
import { useCompactTransitionAnchor } from "./model/use-compact-transition-anchor";
import { useOlderHistoryPull } from "./model/use-older-history-pull";
import { useSendAnchor, useThreadPin } from "./model/use-viewport-arrivals";
import { usePrependAnchor } from "./view/use-prepend-anchor";
import { useTimelineExpansion } from "./view/use-timeline-expansion";
import {
  cancelScrollToBottomRide,
  createScrollToBottomRide,
  startScrollToBottomRide,
  useTimelineViewport,
} from "./view/use-timeline-viewport";
import { useReanchorOnWidthChange } from "./model/width-change-anchor";
import { useStickyThresholds } from "./model/sticky-thresholds";
import { LoadOlderSpinner, LoadOlderKeyboardButton, ColdLoadSpinner, ScrollToBottomButton } from "./overlays";

interface Props {
  items: BackendTimelineItem[];
  compactBuffer?: CompactTransitionBuffer | null;
  threadKey?: string;
  scrollMemoryKey?: string;

  streamingCode?: boolean;

  turnActive?: boolean;

  // meiva:note
  // Page-clock time of the thread's last received event; anchors the running turn's live clock
  // through the gaps where no block is in flight (0 = unknown).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  lastEventAt?: number;

  hasMoreOlder?: boolean;
  // meiva:note
  // An older page in flight is not a display state. `loadingOlder` is the
  // concurrency guard that the pull gesture, the keyboard control, the cold
  // first-viewport fill and compact-history retention all hold;
  // `readerLoadingOlder` says the reader asked for it, and only that presents
  // the loading strip above the messages.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
  loadingOlder?: boolean;
  readerLoadingOlder?: boolean;
  onLoadOlder?: (origin: OlderLoadOrigin) => void;
  retainedItemsMax?: number;

  loading?: boolean;
  loadingNew?: boolean;
  maxWidthCss?: string;
  // meiva:note
  // Reports whether the viewport rests at the latest position (the same measure that shows the
  // scroll-to-bottom control). A thread being left reports false.
  // meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
  onAtBottomChange?: (atBottom: boolean) => void;
}

export const TimelineView = memo(function TimelineView({
  items: canonicalItems,
  compactBuffer = null,
  threadKey,
  scrollMemoryKey,
  streamingCode = false,
  turnActive = false,
  lastEventAt = 0,
  hasMoreOlder = false,
  loadingOlder = false,
  readerLoadingOlder = false,
  onLoadOlder,
  retainedItemsMax = 250,
  loading = false,
  loadingNew = false,
  maxWidthCss,
  onAtBottomChange,
}: Props) {
  const t = useTranslations("timeline");
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
  const viewportRef = useRef<HTMLElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const historySpaceRef = useRef<HTMLDivElement>(null);
  // meiva:note
  // The send anchor's bottom reserve: empty height the reply consumes as it
  // streams in, which is what keeps the reader's own message parked a fixed
  // share down the viewport. It sits outside the content column so it carries
  // no row spacing, and useSendAnchor writes its height directly.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  const sendReserveRef = useRef<HTMLDivElement>(null);

  const pendingRestoreRef = useRef<SavedScroll | null>(scrollMemoryKey ? readScrollMemory(scrollMemoryKey) : null);

  const holdRestoredPositionRef = useRef(!!pendingRestoreRef.current && !pendingRestoreRef.current.followBottom);
  const settledRestoreRef = useRef<SavedScroll | null>(null);
  // meiva:note
  // Consecutive layout passes at the same content height. A bookmark beyond the reachable end is only unreachable once the content has stopped growing - before that, the height is still arriving and waiting is correct.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  const restoreProbeRef = useRef<{ height: number; passes: number }>({ height: -1, passes: 0 });
  const restoreInFlight = useCallback(() => pendingRestoreRef.current !== null || (settledRestoreRef.current !== null && holdRestoredPositionRef.current), []);
  const initialRestorePassRef = useRef(true);

  useLayoutEffect(
    () => () => {
      initialRestorePassRef.current = true;
    },
    [],
  );

  const scrollMemoryRef = useRef<{ entryKey: string; contentKey: string } | null>(null);

  const loadOlderRef = useRef<((origin: OlderLoadOrigin) => void) | undefined>(onLoadOlder);
  const hasMoreOlderRef = useRef(hasMoreOlder);
  const loadingOlderRef = useRef(loadingOlder);
  const itemsRef = useRef(items);
  useLayoutEffect(() => {
    loadOlderRef.current = onLoadOlder;
    hasMoreOlderRef.current = hasMoreOlder;
    loadingOlderRef.current = loadingOlder;
    itemsRef.current = items;
    scrollMemoryRef.current =
      scrollMemoryKey && threadKey !== undefined
        ? { entryKey: scrollMemoryKey, contentKey: threadKey }
        : null;
  });

  const [atBottom, setAtBottom] = useState(true);
  useEffect(() => {
    onAtBottomChange?.(atBottom);
    return () => onAtBottomChange?.(false);
  }, [atBottom, onAtBottomChange]);

  const {
    turnOverrides,
    toggleTurn,
    overrides,
    toggleExpanded,
  } = useTimelineExpansion({
    scrollMemoryKey,
    threadKey,
  });

  const stickyThresholds = useStickyThresholds();

  const [controller] = useState<StickyController>(() =>
    createStickyController(stickyThresholds),
  );
  useReanchorOnWidthChange({
    wrapperRef, entriesRef: scrollMemoryRef, holdRef: holdRestoredPositionRef,
    controller, compactBuffer, canonicalItems,
  });

  useLayoutEffect(() => () => {
    const mem = scrollMemoryRef.current;
    const viewport = viewportRef.current;
    if (mem && viewport && !restoreInFlight()) rememberTimelinePosition(viewport, mem.entryKey, mem.contentKey, controller);
  }, [restoreInFlight, controller]);

  const isFollowing = useCallback(() => !holdRestoredPositionRef.current && controller.isSticky(), [controller]);
  const canonicalItemsRef = useRef(canonicalItems);
  const retainedItemsMaxRef = useRef(retainedItemsMax);
  const scrollToBottomRideRef = useRef(createScrollToBottomRide());

  useEffect(() => () => cancelScrollToBottomRide(scrollToBottomRideRef.current), []);

  const trimFollowing = useCallback((allowDuringRide = false) => {
    if (!controller.isSticky() || holdRestoredPositionRef.current) return false;
    if (!allowDuringRide && scrollToBottomRideRef.current.active) return false;
    const store = useTimelineStore.getState();
    if (store.items !== canonicalItemsRef.current) return false;
    if (store.compactBuffer || store.items.length <= retainedItemsMaxRef.current) return false;
    const before = store.items;
    store.trimOldestWhileFollowing(retainedItemsMaxRef.current);
    return useTimelineStore.getState().items !== before;
  }, [controller]);
  useLayoutEffect(() => {
    canonicalItemsRef.current = canonicalItems;
    retainedItemsMaxRef.current = retainedItemsMax;
    trimFollowing();
  }, [canonicalItems, retainedItemsMax, trimFollowing]);

  // meiva:note
  // The commit that shrinks the content is the only place that knows a clamp is
  // coming and is not itself about to re-pin: by the time the clamp's scroll
  // event dispatches, a later commit has usually added rows again, so
  // `scrollHeight < prevScrollHeight` no longer holds and the move reads as a
  // user scroll-up (#5678). The controller owns the baseline this comparison
  // runs against and drops it for a parked reader, so a reader who is not
  // following pays no layout read here.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  const declareShrink = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    if (!controller.isSticky()) {
      controller.recordContentHeight(null);
      return;
    }
    controller.notifyContentShrunk({
      scrollTop: viewport.scrollTop,
      scrollHeight: viewport.scrollHeight,
      clientHeight: viewport.clientHeight,
    });
  }, [controller, viewportRef]);
  useLayoutEffect(() => {
    declareShrink();
  });

  const [announcedTurnActive, setAnnouncedTurnActive] = useState(turnActive);
  const [turnAnnouncement, setTurnAnnouncement] = useState<string | null>(null);
  if (announcedTurnActive !== turnActive) {
    setAnnouncedTurnActive(turnActive);
    setTurnAnnouncement(turnActive ? t("agentResponding") : t("agentResponseComplete"));
  }

  const measureAtBottom = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    if (scrollToBottomRideRef.current.active) {
      setAtBottom(true);
      return;
    }
    if (holdRestoredPositionRef.current) {
      setAtBottom(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 1);
      return;
    }
    // meiva:note
    // The control's presence follows the measured distance, with one exception:
    // a send anchor holds the bottom up, so the viewport is "at the bottom" by
    // position while the button still has a job (resume following). Hiding it
    // there would leave the reader no way back.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
    setAtBottom(
      !controller.isAnchored() &&
      isAtBottom(
        {
          scrollTop: viewport.scrollTop,
          scrollHeight: viewport.scrollHeight,
          clientHeight: viewport.clientHeight,
        },
        stickyThresholds,
      ),
    );
  }, [controller, stickyThresholds]);

  const pinToBottom = useCallback(
    (viewport: HTMLElement, freshRun = false) => {
      viewport.scrollTop = viewport.scrollHeight;
      controller.notifyPinnedToBottom(
        {
          scrollTop: viewport.scrollTop,
          scrollHeight: viewport.scrollHeight,
          clientHeight: viewport.clientHeight,
        },
        freshRun,
      );
    },
    [controller],
  );

  const coldFillRef = useRef({ pages: 0, open: true, inFlight: false, sawLoading: false });
  const prependEchoRef = useRef(false);

  const scrollByDelta = useCallback((viewport: HTMLElement, delta: number) => {
    viewport.scrollTop += delta;
    prependEchoRef.current = true;
  }, []);

  const { pendingAnchorRef, captureAnchor } = usePrependAnchor({
    viewportRef,
    wrapperRef,
    itemsRef,
    items,
    loadingOlder,
    scrollByDelta,
  });

  // meiva:note
  // A send takes the viewport from any bookmark it was settling: the reader
  // asked for the message they just wrote, so the saved row no longer owns it.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  const cancelBookmark = useCallback(() => {
    cancelScrollToBottomRide(scrollToBottomRideRef.current);
    pendingAnchorRef.current = null;
    pendingRestoreRef.current = null;
    settledRestoreRef.current = null;
    holdRestoredPositionRef.current = false;
  }, [pendingAnchorRef]);

  const { maintain: maintainSendAnchor } = useSendAnchor({
    items,
    viewportRef,
    wrapperRef,
    reserveRef: sendReserveRef,
    controller,
    measureAtBottom,
    cancelBookmark,
  });

  const compactPin = useCompactTransitionAnchor({
    controller,
    viewportRef,
    prependAnchorRef: pendingAnchorRef,
    compactBuffer,
  });

  useLayoutEffect(() => {
    cancelScrollToBottomRide(scrollToBottomRideRef.current);
    coldFillRef.current = { pages: 0, open: true, inFlight: false, sawLoading: false };
    prependEchoRef.current = false;
  }, [threadKey]);
  useEffect(() => {
    const fill = coldFillRef.current;
    if (loadingOlder) fill.sawLoading = true;
    else if (fill.sawLoading) {
      fill.inFlight = false;
      fill.sawLoading = false;
    }
  }, [loadingOlder]);

  const loadOneOlderPage = useCallback((origin: OlderLoadOrigin) => {
    const viewport = viewportRef.current;
    if (!viewport || !hasMoreOlderRef.current || loadingOlderRef.current) return false;
    if (!loadOlderRef.current || pendingAnchorRef.current !== null) return false;
    captureAnchor();
    loadOlderRef.current(origin);
    return true;
  }, [captureAnchor, pendingAnchorRef]);
  // meiva:note
  // The pull gesture and the keyboard control are the reader's only two
  // requests for an older page; every other backward read here is an automatic
  // fill and stays unpresented.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.md
  const pullOlderPage = useCallback(() => loadOneOlderPage("reader"), [loadOneOlderPage]);

  const canPullOlder = useCallback(() =>
    hasMoreOlderRef.current && !loadingOlderRef.current && !!loadOlderRef.current && pendingAnchorRef.current === null,
  [pendingAnchorRef]);
  const closeColdFill = useCallback(() => { coldFillRef.current.open = false; }, []);
  const {
    prepare: prepareHistoryPull, contentHeight: historyContentHeight, offset: olderPullOffset, progress: olderPullProgress, returning: olderPullReturning,
    isActive: historyPullActive, finishLoad: finishHistoryPull, onScroll: pullScroll, onScrollEnd: pullScrollEnd,
    onWheel: pullWheel, onTouchStart: pullTouchStart, onTouchMove: pullTouchMove,
    onTouchEnd: pullTouchEnd, onTouchCancel: cancelPullTouch,
  } = useOlderHistoryPull(threadKey, historySpaceRef, contentRef, canPullOlder, pullOlderPage, closeColdFill);

  const maybeColdFill = useCallback(() => {
    const fill = coldFillRef.current;
    const viewport = viewportRef.current;
    if (!fill.open || !viewport || viewport.clientHeight <= 0 || itemsRef.current.length === 0) return;
    if (historyContentHeight(viewport) > viewport.clientHeight || fill.pages >= 2) {
      fill.open = false;
      return;
    }
    if (fill.inFlight) return;
    if (loadOneOlderPage("background")) {
      fill.inFlight = true;
      fill.pages += 1;
    }
  }, [loadOneOlderPage, historyContentHeight]);

  const bookmarkState = useRef<BookmarkState>({
    pending: pendingRestoreRef,
    settled: settledRestoreRef,
    hold: holdRestoredPositionRef,
    probe: restoreProbeRef,
  }).current;
  const applyPendingRestore = useCallback(
    (viewport: HTMLElement | null) => applyBookmarkRestore({
      state: bookmarkState,
      viewport,
      initialPass: initialRestorePassRef.current,
      threadKey,
      compactBuffer,
      canonicalItems: canonicalItemsRef.current,
      controller,
    }),
    [bookmarkState, compactBuffer, controller, threadKey],
  );

  const settleRestoredPosition = useCallback(
    (viewport: HTMLElement | null, layoutChanged: boolean) => settleRestoredPositionImpl({
      state: bookmarkState,
      viewport,
      layoutChanged,
      threadKey,
      compactBuffer,
      canonicalItems: canonicalItemsRef.current,
    }),
    [bookmarkState, compactBuffer, threadKey],
  );

  useTimelineViewport({
    wrapperRef,
    viewportRef,
    contentRef,
    controller,
    stickyThresholds,
    scrollToBottomRideRef,
    holdRestoredPositionRef,
    pendingRestoreRef,
    restoreProbeRef,
    prependEchoRef,
    coldFillRef,
    scrollMemoryRef,
    restoreInFlight,
    measureAtBottom,
    maybeColdFill,
    pinToBottom,
    trimFollowing,
    prepareHistoryPull,
    historyPullActive,
    pullScroll,
    pullScrollEnd,
    pullWheel,
    pullTouchStart,
    pullTouchMove,
    pullTouchEnd,
    cancelPullTouch,
    applyPendingRestore,
    settleRestoredPosition,
    maintainSendAnchor,
  });

  useThreadPin({
    threadKey,
    controller,
    viewportRef,
    wrapperRef,
    anchorRef: pendingAnchorRef,
    pendingRef: pendingRestoreRef,
    settledRef: settledRestoreRef,
    holdRef: holdRestoredPositionRef,
    pinToBottom,
    setAtBottom,
  });

  useLayoutEffect(() => {
    const viewport = viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ?? null;
    initialRestorePassRef.current = false;
    applyPendingRestore(viewport);
    settleRestoredPosition(viewport, false);
  });

  const prevLoadingOlderReleaseRef = useRef(loadingOlder);
  useLayoutEffect(() => {
    if (prevLoadingOlderReleaseRef.current && !loadingOlder) {
      pendingAnchorRef.current = null;
      const wasPulling = historyPullActive();
      finishHistoryPull();
      const viewport = viewportRef.current;
      if (wasPulling && viewport) controller.notifyRestored({
        scrollTop: viewport.scrollTop, scrollHeight: viewport.scrollHeight, clientHeight: viewport.clientHeight,
      });
    }
    prevLoadingOlderReleaseRef.current = loadingOlder;
  }, [loadingOlder, finishHistoryPull, historyPullActive, controller, pendingAnchorRef]);

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
    pendingAnchor: pendingAnchorRef.current,
    compactPin,
    restoreAnchor:
      !pendingRestoreRef.current?.followBottom && pendingRestoreRef.current?.anchor
        ? { id: pendingRestoreRef.current.anchor.itemId, rank: pendingRestoreRef.current.anchor.rank }
        : null,
    restoreTop: pendingRestoreRef.current?.scrollTop ?? null,
  });

  // meiva:note
  // Expanding an active working block jumps directly to its bottom and follows
  // the tail so the reader sees the latest in-progress step as it streams
  // (user ruling 2026-11-09). Expanding a settled block calls
  // `notifyBlockExpanded` instead so the viewport stays where the reader clicked.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/live-block.md
  const lastGroup = groupEntries.at(-1);
  const liveBlockExpanded = lastGroup !== undefined && lastGroup.isLiveTurn && lastGroup.runExpanded;
  const prevLiveBlockExpandedRef = useRef(false);
  const pendingLiveExpandPinRef = useRef(false);
  useLayoutEffect(() => {
    const opened = liveBlockExpanded && !prevLiveBlockExpandedRef.current;
    prevLiveBlockExpandedRef.current = liveBlockExpanded;
    const shouldFollowOpenedLiveBlock =
      pendingLiveExpandPinRef.current ||
      (opened && (controller.isSticky() || controller.isAnchored()));
    controller.notifyLiveBlock(liveBlockExpanded);
    if (shouldFollowOpenedLiveBlock) {
      pendingLiveExpandPinRef.current = false;
      const viewport =
        viewportRef.current ??
        wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
        null;
      if (viewport) {
        maintainSendAnchor();
        pinToBottom(viewport, true);
        setAtBottom(true);
        startScrollToBottomRide({
          ride: scrollToBottomRideRef.current,
          viewport,
          controller,
          pinToBottom,
          measureAtBottom,
          trimFollowing,
        });
      }
    }
  }, [controller, liveBlockExpanded, maintainSendAnchor, measureAtBottom, pinToBottom, trimFollowing]);

  const handleToggleTurn = useCallback(
    (id: string, currentlyExpanded: boolean) => {
      if (!currentlyExpanded) {
        const isLiveTurnToggle =
          lastGroup !== undefined &&
          lastGroup.isLiveTurn &&
          (lastGroup.groupKey === id ||
            (lastGroup.group.kind === "turn" && lastGroup.group.members[0]?.item.item_id === id));
        if (isLiveTurnToggle) {
          cancelBookmark();
          controller.requestStick();
          maintainSendAnchor();
          setAtBottom(true);
          pendingLiveExpandPinRef.current = true;
        } else {
          cancelScrollToBottomRide(scrollToBottomRideRef.current);
          controller.notifyBlockExpanded();
        }
      }
      toggleTurn(id, currentlyExpanded);
    },
    [cancelBookmark, controller, lastGroup, maintainSendAnchor, toggleTurn],
  );

  const handleScrollToBottom = useCallback(() => {
    const viewport =
      viewportRef.current ??
      wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
      null;
    if (!viewport) return;

    cancelBookmark();
    controller.requestStick();
    maintainSendAnchor();
    setAtBottom(true);
    startScrollToBottomRide({
      ride: scrollToBottomRideRef.current,
      viewport,
      controller,
      pinToBottom,
      measureAtBottom,
      trimFollowing,
    });
  }, [cancelBookmark, controller, maintainSendAnchor, measureAtBottom, pinToBottom, trimFollowing]);

  return (
    <div ref={wrapperRef} className={cn("relative", FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
      <LoadOlderSpinner loadingOlder={readerLoadingOlder} pullProgress={olderPullProgress} pullOffset={olderPullOffset} returning={olderPullReturning} />
      {hasMoreOlder && onLoadOlder ? <LoadOlderKeyboardButton loading={loadingOlder} onClick={pullOlderPage} /> : null}
      <ColdLoadSpinner show={loading && items.length === 0} />
      <ConnectionNotice />
      <ScrollArea
        className="h-full text-[13px] leading-relaxed"
        viewportClassName="[overflow-anchor:none] overscroll-y-contain"
      >
        <div ref={historySpaceRef} data-history-pull-space aria-hidden="true" />
        <div
          ref={contentRef}
          role="log"
          data-timeline-item-count={items.length}
          aria-live="polite"
          aria-relevant="additions"
          style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
          // meiva:note
          // pb-6 provides generous breathing room between the last message /
          // Live Activity Window and the composer's top rule (matching ChatGPT,
          // Gemini, and Claude web layouts), paired with endRef below so the
          // padding applies cleanly inside the log container.
          // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.md
          className={cn("mx-auto w-full px-2 pb-6 space-y-3 sm:px-4", BAR_CLEAR_TOP_PADDING_CLASS, virtualEnabled && "timeline-virtual")}
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
            lastEventAt={lastEventAt}
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
        <div ref={sendReserveRef} data-send-reserve aria-hidden="true" />
      </ScrollArea>
      <ScrollToBottomButton atBottom={atBottom} refreshing={loadingNew && !loadingOlder} onClick={handleScrollToBottom} />
    </div>
  );
});
