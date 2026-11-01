"use client";

// meiva:note
// Compact-history retention (task #3698; user ruling 2026-09-17): a compact
// wholesale-replaces the active timeline, and the new summary must not clear
// the view — all older history pages re-attach automatically above it through
// the same cross-segment fetch as scroll-up. The fetch may need the
// post-compact GET to settle first (`hasMoreOlder` only turns true with it),
// so a pending request retries as the window state changes; misses are
// budgeted, then dropped.
// meiva:link ui/web/src/docs/frontend-state/timeline-cache.meiva.okf.md

import { useCallback, useEffect, useLayoutEffect, useRef } from "react";

import {
  type OlderLoadOrigin,
  type OlderSegmentLoadResult,
} from "./timeline-plan";
import { useTimelineStore } from "./timeline-store";

const RETENTION_MAX_ATTEMPTS = 5;

export type { OlderSegmentLoadResult } from "./timeline-plan";

export function useCompactHistoryRetention(options: {
  /** Open thread; null when no agent is selected. */
  agentId: number | null;
  /** Hidden views suspend history reads; visibility resumes the same intent. */
  isVisible: boolean;
  // meiva:note
  // Older-page fetch; reports whether a completed page has more history.
  // Retention asks for it as a `background` origin, so the page it restores
  // above a compact never presents the loading strip.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.meiva.okf.md
  loadOlderSegment: (origin: OlderLoadOrigin) => Promise<OlderSegmentLoadResult>;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  /** Current item count — window growth re-invokes a blocked attempt. */
  itemCount: number;
  compactEpoch: number;
  compactTailReady: boolean;
}): void {
  const {
    agentId, isVisible, loadOlderSegment, hasMoreOlder, loadingOlder, itemCount,
    compactEpoch, compactTailReady,
  } = options;
  const compactReplaceSeq = useTimelineStore((s) => s.compactReplaceSeq);
  const compactReplaceAgent = useTimelineStore((s) => s.compactReplaceAgent);

  const retentionRef = useRef<{ thread: number; epoch: number; attempts: number } | null>(
    null,
  );
  const retentionInFlightRef = useRef<{ pending: object } | null>(null);
  const ownerRef = useRef<object | null>(null);
  // Selection/visibility ownership is separate from the compact edge. A late
  // completion cannot consume a newer run, including A-to-B-to-A or hide/show.
  useLayoutEffect(() => {
    const owner = agentId !== null && isVisible ? {} : null;
    ownerRef.current = owner;
    retentionInFlightRef.current = null;
    if (retentionRef.current?.thread !== agentId) retentionRef.current = null;
    else retentionRef.current.epoch = compactEpoch;
    return () => { ownerRef.current = null; };
  }, [agentId, isVisible, compactEpoch]);
  const lastRetentionSeqRef = useRef(useTimelineStore.getState().compactReplaceSeq);
  const runRetention = useCallback(() => {
    const pending = retentionRef.current;
    const owner = ownerRef.current;
    if (!isVisible || pending === null || owner === null || retentionInFlightRef.current?.pending === pending) return;
    if (agentId == null || pending.thread !== agentId) {
      retentionRef.current = null;
      return;
    }
    const st = useTimelineStore.getState();
    if (st.activeThreadId !== agentId || st.compactEpoch !== pending.epoch) return;
    if (!st.hasMoreOlder) {
      // meiva:note
      // False before the tail GET is unready; false after it is terminal.
      if (compactTailReady) {
        retentionRef.current = null;
        useTimelineStore.getState().finishCompactTransition(agentId, pending.epoch);
      }
      return;
    }
    if (st.loadingOlder) return;
    const run = { pending };
    retentionInFlightRef.current = run;
    void (async () => {
      try {
        while (ownerRef.current === owner && retentionRef.current === pending) {
          if (useTimelineStore.getState().activeThreadId !== agentId) return;
          const result = await loadOlderSegment("background");
          if (ownerRef.current !== owner || retentionRef.current !== pending) return;
          const current = pending;
          if (result === "aborted") return;
          if (result === "unready" || result === "failed") {
            current.attempts += 1;
            if (current.attempts >= RETENTION_MAX_ATTEMPTS) retentionRef.current = null;
            return;
          }
          if (result === "exhausted") {
            retentionRef.current = null;
            useTimelineStore.getState().finishCompactTransition(agentId, pending.epoch);
            return;
          }
          current.attempts = 0;
          // meiva:note
          // Yield between pages: an unlimited walk must leave room for
          // selection/visibility changes and browser rendering.
          await new Promise<void>((resolve) => setTimeout(resolve, 0));
        }
      } finally {
        if (retentionInFlightRef.current === run) retentionInFlightRef.current = null;
      }
    })();
  }, [agentId, isVisible, loadOlderSegment, compactTailReady]);
  useEffect(() => {
    if (compactReplaceSeq === lastRetentionSeqRef.current) return;
    lastRetentionSeqRef.current = compactReplaceSeq;
    if (agentId == null || compactReplaceAgent !== agentId) return;
    retentionRef.current = {
      thread: agentId,
      epoch: useTimelineStore.getState().compactEpoch,
      attempts: 0,
    };
    runRetention();
  }, [compactReplaceSeq, compactReplaceAgent, agentId, runRetention]);
  // Retry a pending retention as the post-replace window settles: the GET
  // refetch flips hasMoreOlder on, prepends change the item count — either
  // can make a previously-blocked attempt loadable.
  useEffect(() => {
    runRetention();
  }, [hasMoreOlder, loadingOlder, itemCount, compactTailReady, compactEpoch, runRetention]);
}
