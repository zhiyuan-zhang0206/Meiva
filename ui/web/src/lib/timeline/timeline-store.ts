// Selected conversation render state: HTTP snapshots plus live SSE folds.
// A dedicated Zustand store keeps high-frequency timeline updates from waking
// sidebar, spawn-dialog and cluster-banner subscribers. useTimeline coordinates
// its activeThreadId with the UI selection; the stores never cross-read.
// Switching seeds from a retained reading window or HTTP tail snapshot. Only
// the selected view owns live updates; inactive windows are frozen in QueryClient.

"use client";

import { create } from "zustand";

import {
  announceBeforeCompactDisplayChange,
  canonicalCoversBuffer,
  captureCompactTransition,
  type CompactTransitionBuffer,
} from "../layout/compact-transition";
import {
  clearCodeClocks,
  freezeExecClocks,
  freezeReasoningClocks,
  mergeSnapshotWithStreaming,
  sortByItemId,
} from "../fold/timeline";
import {
  applySseEvent,
  crossedUnseenCompact,
  retireLiveCompact,
  type TimelineState,
} from "./timeline-store-events";

export type { LiveCompact, TimelineState } from "./timeline-store-events";

// =============================================================
// Selected timeline reducer
// =============================================================

// Active history remains fully pageable. Switching releases the loaded view;
// it does not delete durable history or retain inactive live buckets.

function announceCompactDisplayChange(
  state: TimelineState,
  nextBuffer: CompactTransitionBuffer | null,
  rankShift = 0,
): void {
  if (
    rankShift === 0 &&
    state.compactBuffer?.epoch === nextBuffer?.epoch &&
    state.compactBuffer?.rows === nextBuffer?.rows
  ) return;
  if (!state.compactBuffer && !nextBuffer) return;
  announceBeforeCompactDisplayChange({
    items: state.items,
    buffer: state.compactBuffer,
    rankShift,
  });
}

export const useTimelineStore = create<TimelineState>()((set, get) => ({
  items: [],
  compactBuffer: null,
  compactEpoch: 0,
  streamingCode: false,
  turnActive: false,
  connectionState: "open",
  tokenUsage: 0,
  reasoningTokens: 0,
  maxContextTokens: 0,
  softCompactTokens: 0,
  hardCompactTokens: 0,
  activeThreadId: null,
  streamingIds: new Set(),
  resetPending: false,
  compactReplaceSeq: 0,
  compactReplaceAgent: null,
  liveCompact: null,
  hasMoreOlder: false,
  loadingOlder: false,
  olderFetchCount: 0,
  scrollToBottomRequest: 0,

  requestScrollToBottom: () => set((s) => ({ scrollToBottomRequest: s.scrollToBottomRequest + 1 })),
  invalidateCompactEpoch: () => set((s) => {
    const compactBuffer = s.compactBuffer
      ? { ...s.compactBuffer, epoch: s.compactEpoch + 1 }
      : null;
    announceCompactDisplayChange(s, compactBuffer);
    return { compactEpoch: s.compactEpoch + 1, compactBuffer };
  }),
  finishCompactTransition: (threadId, epoch) => set((s) => {
    if (s.compactBuffer?.threadId !== threadId || s.compactBuffer.epoch !== epoch) return {};
    announceCompactDisplayChange(s, null);
    return { compactBuffer: null };
  }),

  processSseEvent: (ev, nowMs = 1) => {
    // agent_spawned / agent_updated belong to sidebar state (TanStack
    // Query cache), not the timeline. The root fold (the single
    // cache writer) handles them; the timeline store ignores them outright.
    if (ev.role === "agent_spawned" || ev.role === "agent_updated") return;
    set((s) => {
      const patch = applySseEvent(s, ev, nowMs);
      if (patch.compactReplaceSeq !== undefined) {
        announceCompactDisplayChange(s, patch.compactBuffer ?? null, patch.compactReplaceSeq - s.compactReplaceSeq);
      }
      return patch;
    });
  },

  processSseEventBatch: (events, nowMs = 1) => {
    let changed = false;
    const merged: Partial<TimelineState> = {};
    let working = get();
    for (const ev of events) {
      if (ev.role === "agent_spawned" || ev.role === "agent_updated") continue;
      const patch = applySseEvent(working, ev, nowMs);
      if (Object.keys(patch).length === 0) continue;
      changed = true;
      Object.assign(merged, patch);
      working = { ...working, ...patch };
    }
    if (!changed) return;
    if (working.compactReplaceSeq !== get().compactReplaceSeq) {
      announceCompactDisplayChange(
        get(), working.compactBuffer,
        working.compactReplaceSeq - get().compactReplaceSeq,
      );
    }
    // One set() per frame: every event's fold already applied to `working`;
    // `merged` carries the cumulative patch. Synchronous — no other set()
    // can interleave between get() above and this commit.
    set(merged);
  },

  processConnectionEvent: (ev) => {
    set({ connectionState: ev.type });
    if (ev.type === "closed") {
      // SSE disconnect = streaming interrupted. Keep the partial flag
      // (showing the content as "..." ellipsis remains reasonable), add
      // an interrupted flag so the timeline renders an extra
      // "streaming interrupted" hint that distinguishes "message simply
      // ends here" from "disconnect, content may be incomplete". After
      // reconnect, SSE keeps pushing deltas and naturally flips
      // interrupted off (the delta reducer does not carry this flag).
      set((s) => ({
        streamingCode: false,
        turnActive: false,
        items: s.items.some((it) => it.partial)
          ? s.items.map((it) =>
              it.partial && !it.interrupted ? { ...it, interrupted: true } : it,
            )
          : s.items,
      }));
    } else if (ev.type === "open") {
      // Reconnected; clear the interrupted flag — the previously
      // partial items will keep appending via deltas, so the "interrupted"
      // hint no longer applies.
      set((s) => {
        const compactBuffer = s.compactBuffer
          ? { ...s.compactBuffer, epoch: s.compactEpoch + 1 }
          : null;
        announceCompactDisplayChange(s, compactBuffer);
        return {
          items: s.items.some((it) => it.interrupted)
            ? s.items.map((it) => (it.interrupted ? { ...it, interrupted: false } : it))
            : s.items,
          // Reopening requests a fresh snapshot and releases the compact guard.
          // The existing wire contract cannot prove its commit order.
          resetPending: false,
          compactEpoch: s.compactBuffer ? s.compactEpoch + 1 : s.compactEpoch,
          compactBuffer,
        };
      });
    }
  },

  reloadSnapshot: (snapshot, msg_count, hasMoreOlder) => {
    set((s) => {
      // A compact changes item coordinates. During the reset window only
      // a snapshot showing that rewrite can replace the old view.
      const crossedCompact = crossedUnseenCompact(s.items, snapshot);
      if (s.resetPending && !crossedCompact) {
        return { hasMoreOlder };
      }
      const snapshotIds = new Set(snapshot.map((it) => it.item_id));
      const merged = mergeSnapshotWithStreaming(
        crossedCompact ? [] : s.items,
        snapshot,
        msg_count,
        s.streamingIds,
      );
      const epoch = crossedCompact ? s.compactEpoch + 1 : s.compactEpoch;
      const pendingBuffer = crossedCompact && s.activeThreadId !== null
        ? captureCompactTransition(s.items, s.compactBuffer, s.activeThreadId, epoch, true)
        : s.compactBuffer && !crossedCompact
          ? { ...s.compactBuffer, tailReady: true }
          : null;
      const compactBuffer = pendingBuffer &&
        (canonicalCoversBuffer(pendingBuffer, merged) || !hasMoreOlder)
          ? null
          : pendingBuffer;
      announceCompactDisplayChange(s, compactBuffer, crossedCompact ? 1 : 0);
      return {
        // committed ids drop out of streamingIds; still-streaming ones stay
        streamingIds: new Set([...s.streamingIds].filter((id) => !snapshotIds.has(id))),
        items: merged,
        compactEpoch: epoch,
        compactBuffer,
        hasMoreOlder,
        resetPending: crossedCompact ? false : s.resetPending,
        // Reconnect can discover the rewrite without any compact SSE event.
        // A cold snapshot seeds a view; only an existing view needs retention.
        compactReplaceSeq: crossedCompact && s.items.length > 0 ? s.compactReplaceSeq + 1 : s.compactReplaceSeq,
        compactReplaceAgent: crossedCompact && s.items.length > 0 ? s.activeThreadId : s.compactReplaceAgent,
        liveCompact: retireLiveCompact(s.liveCompact, merged),
      };
    });
  },

  switchThread: (agentId, cached, hasMoreOlder, buffer = null) => {
    if (buffer && buffer.threadId !== agentId) throw new Error("A retained timeline buffer must belong to its selected agent");
    set((s) => ({
      activeThreadId: agentId,
      items: cached ?? [],
      compactBuffer: buffer ? { ...buffer, epoch: s.compactEpoch + 1 } : null,
      compactEpoch: s.compactEpoch + 1,
      streamingIds: new Set(),
      streamingCode: false,
      turnActive: false,
      hasMoreOlder: cached !== null && hasMoreOlder,
      olderFetchCount: 0,
      resetPending: false,
      liveCompact: null,
      tokenUsage: 0,
      reasoningTokens: 0,
      maxContextTokens: 0,
      softCompactTokens: 0,
      hardCompactTokens: 0,
      loadingOlder: false,
    }));
  },

  applyTokenUsage: (input, reasoning, maxContext, softCompact, hardCompact) =>
    set({
      tokenUsage: input,
      reasoningTokens: reasoning,
      maxContextTokens: maxContext,
      softCompactTokens: softCompact,
      hardCompactTokens: hardCompact,
    }),

  beginLoadOlder: () => set({ loadingOlder: true }),

  prependOlder: (older, hasMoreOlder) => {
    set((s) => {
      const existing = new Set(s.items.map((it) => it.item_id));
      const fresh = older.filter((it) => !existing.has(it.item_id));
      if (fresh.length === 0 && hasMoreOlder) {
        // The page added nothing yet claims older history still exists: the
        // gateway returned only standing context it could not cross (a
        // version mix — an older gateway does not recognize the re-attached
        // head notes as crossing material). Stop paging instead of looping
        // on the same cursor forever.
        announceCompactDisplayChange(s, null);
        return { hasMoreOlder: false, loadingOlder: false, compactBuffer: null };
      }
      const merged = fresh.length ? sortByItemId([...fresh, ...s.items]) : s.items;
      const compactBuffer = s.compactBuffer &&
        (canonicalCoversBuffer(s.compactBuffer, merged) || !hasMoreOlder)
          ? null
          : s.compactBuffer;
      announceCompactDisplayChange(s, compactBuffer);
      return {
        items: merged,
        compactBuffer,
        hasMoreOlder,
        loadingOlder: false,
      };
    });
  },

  trimOldestWhileFollowing: (maxItems) => {
    const state = get();
    // The compact transition buffer owns a reader anchor across re-keying.
    // Leave that short transition intact; trim once it is released.
    if (state.compactBuffer || state.items.length <= maxItems) return;
    set({
      items: state.items.slice(-maxItems),
      hasMoreOlder: true,
      // A later scroll-up re-reads the evicted tail in small pages.
      olderFetchCount: 0,
    });
  },

  incrementOlderFetchCount: () => set((s) => ({ olderFetchCount: s.olderFetchCount + 1 })),

  clearPartialFlags: () => {
    set((s) => ({
      items: s.items.some((it) => it.partial)
        ? s.items.map((it) => (it.partial ? { ...it, partial: false } : it))
        : s.items,
    }));
  },

  settleTurn: (nowMs = 1) => {
    set((s) => {
      const hasLiveClock = s.items.some(
        (it) =>
          (it.reasoningStartedAt != null && it.reasoning_ms == null) ||
          it.codeStartedAt != null ||
          (it.execStartedAt != null && it.exec_ms == null) ||
          (it.kind === "code_output" && it.payload === "" && it.exec_ms == null),
      );
      if (!s.turnActive && !s.streamingCode && s.streamingIds.size === 0 && !hasLiveClock) {
        return {};
      }
      const cleaned = s.items.filter(
        (it) => !it.llmWaiting && !(it.kind === "code_output" && it.payload === "" && it.exec_ms == null),
      );
      const items = hasLiveClock
        ? freezeExecClocks(clearCodeClocks(freezeReasoningClocks(cleaned, nowMs), nowMs), nowMs)
        : cleaned;
      return {
        turnActive: false,
        streamingCode: false,
        streamingIds: new Set(),
        items,
      };
    });
  },
}));
