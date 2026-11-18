import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import type { SavedScroll } from "@/lib/layout/scroll-memory";
import type { StickyController } from "@/lib/layout/sticky";
import type { OlderLoadOrigin } from "@/lib/timeline/use-timeline";

import {
  matchReloadBookmark,
  oldestReloadCursorId,
  readReloadBookmark,
  RELOAD_RESTORE_MAX_OLDER_ITEMS,
  RELOAD_RESTORE_MAX_OLDER_PAGES,
  RELOAD_RESTORE_SEARCH_TIMEOUT_MS,
  reloadBookmarkToSavedScroll,
  removeReloadBookmark,
  type ReloadReadingBookmark,
  type ReloadRestoreConfig,
} from "./reading-position";
import { settleSavedTimelineScroll } from "./restore-settle";
import { applySavedTimelineScroll } from "./use-timeline-window";

// meiva:note
// A bookmark restore owns the viewport until it lands (applyBookmarkRestore), keeps the layout from pinning meanwhile (the hold), and then verifies its own target on later layout passes (settleRestoredPosition). Two escapes keep a bookmark from owning the viewport forever: a bookmark past the end of a page that stopped growing lands on the end, and a settle that reaches its target releases the hold so following and the position save resume.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md

export interface BookmarkState {
  pending: RefObject<SavedScroll | null>;
  settled: RefObject<SavedScroll | null>;
  hold: RefObject<boolean>;
  // meiva:note
  // Consecutive layout passes at one content height: a bookmark beyond the reachable end is only unreachable once the content stopped growing.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  probe: RefObject<{ height: number; passes: number }>;
}

interface ApplyArgs {
  state: BookmarkState;
  viewport: HTMLElement | null;
  initialPass: boolean;
  loading: boolean;
  threadKey: string | undefined;
  compactBuffer: CompactTransitionBuffer | null;
  canonicalItems: readonly BackendTimelineItem[];
  controller: StickyController;
  tailHeight: (viewport: HTMLElement) => number;
  collapseReserve: HTMLElement | null;
}

const snapshot = (viewport: HTMLElement) => ({
  scrollTop: viewport.scrollTop,
  scrollHeight: viewport.scrollHeight,
  clientHeight: viewport.clientHeight,
});

export function applyBookmarkRestore({
  state, viewport, initialPass, loading, threadKey, compactBuffer, canonicalItems, controller, tailHeight, collapseReserve,
}: ApplyArgs): void {
  const saved = state.pending.current;
  if (initialPass || loading || !saved || !viewport) return;
  if (threadKey === undefined) return;
  if (saved.contentKey !== threadKey) {
    state.pending.current = null;
    state.hold.current = false;
    return;
  }
  const naturalHeight = tailHeight(viewport);
  if (!applySavedTimelineScroll(viewport, saved, compactBuffer, canonicalItems, naturalHeight, collapseReserve)) {
    const max = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    const probe = state.probe.current;
    if (probe.passes >= 2 && probe.height === viewport.scrollHeight && saved.scrollTop > max) {
      // meiva:note
      // Older rows were trimmed while away: the bookmark lies past the end of a page that stopped growing. Waiting cannot reach it - land on the end and hand the viewport back, or the hold silences following and every later send for good.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
      viewport.scrollTop = max;
      if (saved.followBottom) controller.notifyRestored(snapshot(viewport));
      else controller.notifyAnchored(snapshot(viewport));
      state.pending.current = null;
      state.settled.current = null;
      state.hold.current = false;
      return;
    }
    controller.notifyRestored(snapshot(viewport));
    return;
  }
  if (saved.followBottom) {
    controller.notifyPinnedToBottom({ ...snapshot(viewport), scrollHeight: naturalHeight }, true);
  } else {
    controller.notifyAnchored(snapshot(viewport));
  }
  state.pending.current = null;
  state.settled.current = saved.anchor ? saved : null;
  if (!saved.anchor) state.hold.current = false;
}

interface SettleArgs {
  state: BookmarkState;
  viewport: HTMLElement | null;
  layoutChanged: boolean;
  threadKey: string | undefined;
  compactBuffer: CompactTransitionBuffer | null;
  canonicalItems: readonly BackendTimelineItem[];
}

export function settleRestoredPosition({
  state, viewport, layoutChanged, threadKey, compactBuffer, canonicalItems,
}: SettleArgs): void {
  const saved = state.settled.current;
  if (!saved || !viewport) return;
  if (state.pending.current !== null || !state.hold.current || threadKey === undefined || saved.contentKey !== threadKey) {
    state.settled.current = null;
    return;
  }
  if (layoutChanged && settleSavedTimelineScroll(viewport, saved, compactBuffer, canonicalItems)) {
    state.settled.current = null;
    state.hold.current = false;
  }
}

type ReloadPhase =
  | { readonly kind: "idle"; readonly threadKey: string | undefined; readonly seq: number }
  | { readonly kind: "waiting_settings"; readonly threadKey: string; readonly seq: number }
  | {
      readonly kind: "searching";
      readonly threadKey: string;
      readonly seq: number;
      readonly bookmark: ReloadReadingBookmark;
      readonly baselineOldestId: string | null;
      readonly baselinePrefixCount: number;
      readonly pagesLoaded: number;
      readonly prependedItems: number;
      readonly requestedBeforeId: string | null;
      readonly sawLoading: boolean;
    }
  | {
      readonly kind: "matched";
      readonly threadKey: string;
      readonly seq: number;
      readonly saved: SavedScroll;
    }
  | {
      readonly kind: "abandoned";
      readonly threadKey: string;
      readonly seq: number;
      readonly clearStoredBookmark: boolean;
    };

function countPrependedBeforeBaseline(
  items: readonly BackendTimelineItem[],
  baselineOldestId: string,
  baselinePrefixCount: number,
): number | null {
  const idx = items.findIndex((item) => item.item_id === baselineOldestId);
  return idx >= 0 ? Math.max(0, idx - baselinePrefixCount) : null;
}

export interface InitialTimelineBookmark {
  readonly inMemorySaved: SavedScroll | null;
  readonly initialReloadBookmark: ReloadReadingBookmark | null;
  readonly savedPosition: SavedScroll | null;
}

export function resolveInitialTimelineBookmark(
  inMemorySaved: SavedScroll | null,
  threadKey: string | undefined,
  reloadRestore: ReloadRestoreConfig,
): InitialTimelineBookmark {
  if (inMemorySaved !== null) {
    return {
      inMemorySaved,
      initialReloadBookmark: null,
      savedPosition: inMemorySaved,
    };
  }
  if (threadKey === undefined || reloadRestore.state === "disabled") {
    return {
      inMemorySaved: null,
      initialReloadBookmark: null,
      savedPosition: null,
    };
  }
  if (reloadRestore.state === "loading") {
    return {
      inMemorySaved: null,
      initialReloadBookmark: null,
      savedPosition: { contentKey: threadKey, scrollTop: 0, followBottom: false },
    };
  }
  const bookmark = readReloadBookmark(reloadRestore.session, threadKey, reloadRestore.now());
  return {
    inMemorySaved: null,
    initialReloadBookmark: bookmark,
    savedPosition: bookmark ? reloadBookmarkToSavedScroll(threadKey, bookmark) : null,
  };
}

function initReloadPhase(
  inMemorySaved: SavedScroll | null,
  threadKey: string | undefined,
  reloadRestore: ReloadRestoreConfig,
  seq: number,
  preloadedBookmark?: ReloadReadingBookmark | null,
): ReloadPhase {
  if (inMemorySaved !== null && inMemorySaved.contentKey === threadKey) {
    return { kind: "idle", threadKey, seq };
  }
  if (threadKey === undefined || reloadRestore.state === "disabled") {
    return { kind: "idle", threadKey, seq };
  }
  if (reloadRestore.state === "loading") {
    return { kind: "waiting_settings", threadKey, seq };
  }
  const bookmark =
    preloadedBookmark !== undefined
      ? preloadedBookmark
      : readReloadBookmark(reloadRestore.session, threadKey, reloadRestore.now());
  if (bookmark === null) {
    return { kind: "idle", threadKey, seq };
  }
  return {
    kind: "searching",
    threadKey,
    seq,
    bookmark,
    baselineOldestId: null,
    baselinePrefixCount: 0,
    pagesLoaded: 0,
    prependedItems: 0,
    requestedBeforeId: null,
    sawLoading: false,
  };
}

// meiva:note
// Coordinate cross-reload bookmark resolution on the single live timeline:
// in-memory scrollMemory wins within the open page; otherwise a sessionStorage
// bookmark resolves against the initial tail window or up to 5 background older
// pages under ColdLoadSpinner, falling back silently to the live tail if
// unreachable or interrupted by reader navigation.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function useReloadBookmarkRestore({
  inMemorySaved,
  initialReloadBookmark,
  threadKey,
  reloadRestore,
  loading,
  restoreReady,
  canonicalItems,
  compactBuffer,
  hasMoreOlder,
  loadingOlder,
  onLoadOlder,
  onCancelBackgroundLoadOlder,
  pendingRef,
  settledRef,
  holdRef,
  probeRef,
  pendingAnchorRef,
  viewportRef,
  wrapperRef,
  pinToBottom,
  trimFollowing,
}: {
  inMemorySaved: SavedScroll | null;
  initialReloadBookmark: ReloadReadingBookmark | null;
  threadKey: string | undefined;
  reloadRestore: ReloadRestoreConfig;
  loading: boolean;
  restoreReady: boolean;
  canonicalItems: readonly BackendTimelineItem[];
  compactBuffer: CompactTransitionBuffer | null;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder?: (origin: OlderLoadOrigin) => void;
  onCancelBackgroundLoadOlder?: () => boolean;
  pendingRef: RefObject<SavedScroll | null>;
  settledRef: RefObject<SavedScroll | null>;
  holdRef: RefObject<boolean>;
  probeRef: RefObject<{ height: number; passes: number }>;
  pendingAnchorRef: RefObject<{ id: string; rank: number; frontId: string | null; docTop: number } | null>;
  viewportRef: RefObject<HTMLElement | null>;
  wrapperRef: RefObject<HTMLDivElement | null>;
  pinToBottom: (viewport: HTMLElement, freshRun?: boolean) => void;
  trimFollowing: (allowDuringRide?: boolean) => boolean;
}) {
  const [phase, setPhase] = useState<ReloadPhase>(() =>
    initReloadPhase(inMemorySaved, threadKey, reloadRestore, 1, initialReloadBookmark),
  );

  if (phase.threadKey !== threadKey) {
    setPhase(initReloadPhase(inMemorySaved, threadKey, reloadRestore, phase.seq + 1));
  } else if (reloadRestore.state === "disabled") {
    if (phase.kind === "waiting_settings" || phase.kind === "searching") {
      setPhase({
        kind: "abandoned",
        threadKey: phase.threadKey,
        seq: phase.seq + 1,
        clearStoredBookmark: false,
      });
    }
  } else if (phase.kind === "waiting_settings" && reloadRestore.state === "enabled") {
    const bookmark = readReloadBookmark(
      reloadRestore.session,
      phase.threadKey,
      reloadRestore.now(),
    );
    if (bookmark !== null) {
      setPhase({
        kind: "searching",
        threadKey: phase.threadKey,
        seq: phase.seq + 1,
        bookmark,
        baselineOldestId: null,
        baselinePrefixCount: 0,
        pagesLoaded: 0,
        prependedItems: 0,
        requestedBeforeId: null,
        sawLoading: false,
      });
    } else {
      setPhase({
        kind: "abandoned",
        threadKey: phase.threadKey,
        seq: phase.seq + 1,
        clearStoredBookmark: false,
      });
    }
  } else if (phase.kind === "searching" && !loading && restoreReady) {
    const match = matchReloadBookmark(phase.bookmark, canonicalItems, compactBuffer);
    if (match !== null) {
      setPhase({
        kind: "matched",
        threadKey: phase.threadKey,
        seq: phase.seq + 1,
        saved: reloadBookmarkToSavedScroll(phase.threadKey, phase.bookmark, match),
      });
    } else {
      const maxPages =
        reloadRestore.state === "enabled"
          ? (reloadRestore.maxOlderPages ?? RELOAD_RESTORE_MAX_OLDER_PAGES)
          : RELOAD_RESTORE_MAX_OLDER_PAGES;
      const maxItems =
        reloadRestore.state === "enabled"
          ? (reloadRestore.maxOlderItems ?? RELOAD_RESTORE_MAX_OLDER_ITEMS)
          : RELOAD_RESTORE_MAX_OLDER_ITEMS;
      const currentOldest = oldestReloadCursorId(canonicalItems);
      const baselineOldestId = phase.baselineOldestId ?? currentOldest;
      const baselinePrefixCount =
        phase.baselineOldestId !== null
          ? phase.baselinePrefixCount
          : baselineOldestId !== null
            ? Math.max(0, canonicalItems.findIndex((item) => item.item_id === baselineOldestId))
            : 0;

      if (phase.requestedBeforeId !== null) {
        if (loadingOlder && !phase.sawLoading) {
          setPhase({
            ...phase,
            baselineOldestId,
            baselinePrefixCount,
            sawLoading: true,
          });
        } else if (
          !loadingOlder &&
          (phase.sawLoading ||
            (currentOldest !== null && currentOldest !== phase.requestedBeforeId))
        ) {
          const progressed =
            currentOldest !== null && currentOldest !== phase.requestedBeforeId;
          const pagesLoaded = phase.pagesLoaded + 1;
          const measuredPrepended =
            baselineOldestId !== null
              ? countPrependedBeforeBaseline(
                  canonicalItems,
                  baselineOldestId,
                  baselinePrefixCount,
                )
              : null;
          const prependedItems = measuredPrepended ?? phase.prependedItems;
          const canContinue =
            progressed &&
            hasMoreOlder &&
            Boolean(onLoadOlder) &&
            pagesLoaded < maxPages &&
            prependedItems < maxItems;
          if (canContinue) {
            setPhase({
              ...phase,
              seq: phase.seq + 1,
              baselineOldestId,
              baselinePrefixCount,
              pagesLoaded,
              prependedItems,
              requestedBeforeId: currentOldest,
              sawLoading: false,
            });
          } else {
            setPhase({
              kind: "abandoned",
              threadKey: phase.threadKey,
              seq: phase.seq + 1,
              clearStoredBookmark: true,
            });
          }
        }
      } else {
        const canStart =
          canonicalItems.length > 0 &&
          hasMoreOlder &&
          Boolean(onLoadOlder) &&
          currentOldest !== null &&
          phase.pagesLoaded < maxPages &&
          phase.prependedItems < maxItems;
        if (canStart) {
          setPhase({
            ...phase,
            seq: phase.seq + 1,
            baselineOldestId,
            baselinePrefixCount,
            requestedBeforeId: currentOldest,
            sawLoading: loadingOlder,
          });
        } else {
          setPhase({
            kind: "abandoned",
            threadKey: phase.threadKey,
            seq: phase.seq + 1,
            clearStoredBookmark: true,
          });
        }
      }
    }
  }

  const activeSearchRequestRef = useRef(false);
  useLayoutEffect(() => {
    activeSearchRequestRef.current =
      phase.kind === "searching" && phase.requestedBeforeId !== null;
  }, [phase]);

  const cancelReloadSearch = useCallback((): boolean => {
    let abortedBackground = false;
    if (activeSearchRequestRef.current) {
      activeSearchRequestRef.current = false;
      abortedBackground = onCancelBackgroundLoadOlder?.() === true;
      pendingAnchorRef.current = null;
    }
    setPhase((prev) =>
      prev.kind === "waiting_settings" || prev.kind === "searching"
        ? { kind: "idle", threadKey: prev.threadKey, seq: prev.seq + 1 }
        : prev,
    );
    return abortedBackground;
  }, [onCancelBackgroundLoadOlder, pendingAnchorRef]);

  const searchTimeoutMs =
    reloadRestore.state === "enabled"
      ? (reloadRestore.searchTimeoutMs ?? RELOAD_RESTORE_SEARCH_TIMEOUT_MS)
      : RELOAD_RESTORE_SEARCH_TIMEOUT_MS;
  const waitingOrSearching =
    phase.kind === "waiting_settings" ||
    (phase.kind === "searching" && !loading && restoreReady);
  const phaseRequestedBeforeId =
    phase.kind === "searching" ? phase.requestedBeforeId : null;
  useEffect(() => {
    if (!waitingOrSearching || searchTimeoutMs <= 0) return;
    const timedSeq = phase.seq;
    const hadRequest =
      phase.kind === "searching" && phaseRequestedBeforeId !== null;
    const timer = setTimeout(() => {
      if (hadRequest) {
        activeSearchRequestRef.current = false;
        onCancelBackgroundLoadOlder?.();
        pendingAnchorRef.current = null;
      }
      setPhase((prev) =>
        (prev.kind === "waiting_settings" || prev.kind === "searching") &&
        prev.seq === timedSeq
          ? {
              kind: "abandoned",
              threadKey: prev.threadKey,
              seq: prev.seq + 1,
              clearStoredBookmark: false,
            }
          : prev,
      );
    }, searchTimeoutMs);
    return () => clearTimeout(timer);
  }, [
    waitingOrSearching,
    phase.kind,
    phase.seq,
    phaseRequestedBeforeId,
    searchTimeoutMs,
    onCancelBackgroundLoadOlder,
    pendingAnchorRef,
  ]);

  const appliedSeqRef = useRef(0);
  useLayoutEffect(() => {
    if (appliedSeqRef.current === phase.seq) return;
    if (phase.kind === "searching") {
      appliedSeqRef.current = phase.seq;
      pendingRef.current = reloadBookmarkToSavedScroll(
        phase.threadKey,
        phase.bookmark,
      );
      holdRef.current = true;
      return;
    }
    if (phase.kind === "matched") {
      appliedSeqRef.current = phase.seq;
      pendingRef.current = phase.saved;
      settledRef.current = null;
      holdRef.current = true;
      probeRef.current = { height: -1, passes: 0 };
      return;
    }
    if (phase.kind === "abandoned") {
      appliedSeqRef.current = phase.seq;
      pendingAnchorRef.current = null;
      pendingRef.current = null;
      settledRef.current = null;
      holdRef.current = false;
      if (phase.clearStoredBookmark && reloadRestore.state === "enabled") {
        removeReloadBookmark(
          reloadRestore.session,
          phase.threadKey,
          reloadRestore.now(),
        );
      }
      const viewport =
        viewportRef.current ??
        wrapperRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]') ??
        null;
      if (viewport) {
        pinToBottom(viewport, true);
        if (trimFollowing(true)) pinToBottom(viewport, true);
      }
    }
  }, [
    holdRef,
    pendingAnchorRef,
    pendingRef,
    phase,
    pinToBottom,
    probeRef,
    reloadRestore,
    settledRef,
    trimFollowing,
    viewportRef,
    wrapperRef,
  ]);

  const requestedBeforeId = phase.kind === "searching" ? phase.requestedBeforeId : null;
  const requestSeq = phase.kind === "searching" ? phase.seq : 0;
  useEffect(() => {
    if (requestedBeforeId === null || loadingOlder) return;
    onLoadOlder?.("background");
  }, [requestedBeforeId, requestSeq, loadingOlder, onLoadOlder]);

  const reloadBlocking =
    phase.kind === "waiting_settings" || phase.kind === "searching";
  const reloadSearchingOlder =
    phase.kind === "searching" && !loading && restoreReady;
  const hasSavedPosition =
    (inMemorySaved !== null && inMemorySaved.contentKey === threadKey) ||
    phase.kind === "waiting_settings" ||
    phase.kind === "searching" ||
    phase.kind === "matched";
  const activeSavedScroll =
    phase.kind === "matched"
      ? phase.saved
      : phase.kind === "searching"
        ? reloadBookmarkToSavedScroll(phase.threadKey, phase.bookmark)
        : inMemorySaved !== null && inMemorySaved.contentKey === threadKey
          ? inMemorySaved
          : null;

  return {
    reloadBlocking,
    reloadSearchingOlder,
    hasSavedPosition,
    activeSavedScroll,
    cancelReloadSearch,
  };
}
