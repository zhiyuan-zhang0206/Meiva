import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useTimelineStore } from "../timeline-store";
import type { BackendTimelineItem } from "../../contracts/types";

let idCounter = 0;
function item(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    item_id: `s.${++idCounter}`,
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

function resetStore(): void {
  useTimelineStore.setState({
    activeThreadId: 42,
    items: [],
    compactBuffer: null,
    compactEpoch: 0,
    streamingIds: new Set(),
    streamingCode: false,
    turnActive: false,
    connectionState: "open",
    tokenUsage: 0,
    reasoningTokens: 0,
    maxContextTokens: 0,
    hasMoreOlder: false,
    loadingOlder: false,
    olderFetchCount: 0,
    scrollToBottomRequest: 0,
    liveCompact: null,
    compactReplaceSeq: 0,
    compactReplaceAgent: null,
  });
}

beforeEach(() => {
  resetStore();
});

describe("applyTokenUsage", () => {
  it("writes all token fields atomically", () => {
    act(() => {
      useTimelineStore.setState({ tokenUsage: 1, reasoningTokens: 2, maxContextTokens: 3 });
      useTimelineStore.getState().applyTokenUsage(1200, 300, 200000, 120000, 160000);
    });
    const s = useTimelineStore.getState();
    expect(s.tokenUsage).toBe(1200);
    expect(s.reasoningTokens).toBe(300);
    expect(s.maxContextTokens).toBe(200000);
    expect(s.softCompactTokens).toBe(120000);
    expect(s.hardCompactTokens).toBe(160000);
  });

  it("resets all token fields to 0 (cold path)", () => {
    act(() => {
      useTimelineStore.setState({
        tokenUsage: 900,
        reasoningTokens: 80,
        maxContextTokens: 128000,
        softCompactTokens: 76800,
        hardCompactTokens: 102400,
      });
      useTimelineStore.getState().applyTokenUsage(0, 0, 0, 0, 0);
    });
    const s = useTimelineStore.getState();
    expect(s.tokenUsage).toBe(0);
    expect(s.reasoningTokens).toBe(0);
    expect(s.maxContextTokens).toBe(0);
    expect(s.softCompactTokens).toBe(0);
    expect(s.hardCompactTokens).toBe(0);
  });
});

describe("scrollToBottomRequest send signal", () => {
  it("requestScrollToBottom increments the counter (send path)", () => {
    act(() => {
      useTimelineStore.setState({ scrollToBottomRequest: 5 });
      useTimelineStore.getState().requestScrollToBottom();
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(6);
  });

  it("switchThread preserves it (hot-cache agent switch)", () => {
    act(() => {
      useTimelineStore.setState({ scrollToBottomRequest: 2 });
      useTimelineStore.getState().switchThread(8, [item({ payload: "x" })], false);
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(2);
  });

  it("reloadSnapshot does NOT bump it (mid-stream refresh must not move the reader)", () => {
    act(() => {
      useTimelineStore.setState({ scrollToBottomRequest: 4 });
      useTimelineStore.getState().reloadSnapshot([item({ item_id: "0.0", payload: "s" })], 1, false);
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(4);
  });

  it("prependOlder does NOT bump it (scroll-up load-older must not move the reader)", () => {
    act(() => {
      useTimelineStore.setState({
        items: [item({ item_id: "5.0", payload: "curr" })],
        scrollToBottomRequest: 7,
        hasMoreOlder: true,
        loadingOlder: true,
      });
      useTimelineStore.getState().prependOlder([item({ item_id: "1.0", payload: "old" })], false);
    });
    expect(useTimelineStore.getState().scrollToBottomRequest).toBe(7);
  });
});

describe("reloadSnapshot", () => {
  it("retains an oversized snapshot in full and honors backend has_more", () => {

    const snapshot = Array.from({ length: 6_001 }, (_, index) =>
      item({ item_id: `${index + 1}.0`, payload: `snapshot ${index + 1}` }),
    );

    act(() => {
      useTimelineStore.getState().reloadSnapshot(snapshot, snapshot.length, true);
    });

    const state = useTimelineStore.getState();
    expect(state.items).toHaveLength(6_001);
    expect(state.items[0].item_id).toBe("1.0");
    expect(state.items.at(-1)?.item_id).toBe("6001.0");
    expect(state.hasMoreOlder).toBe(true);
  });

  it("hard reset: stable-id partial discarded when not in snapshot, replaced by snapshot", () => {

    const partial = item({ item_id: "9.9", payload: "streaming...", partial: true });
    act(() => {
      useTimelineStore.setState({ items: [partial] });
      const snapshot = [item({ item_id: "1.0", kind: "inbound_chat", payload: "msg" })];
      useTimelineStore.getState().reloadSnapshot(snapshot, 0, false);
    });
    const after = useTimelineStore.getState().items;
    expect(after.length).toBe(1);
    expect(after[0].item_id).toBe("1.0");

    expect(after.some((i) => i.item_id === "9.9")).toBe(false);
  });

  it("cold thread with one streaming item at msg_idx == msg_count: snapshot still merges in the missing history (#1142)", () => {
    act(() => {

      useTimelineStore.getState().processSseEvent({ role: "code_start", agent_id: 42, item_id: "5.0" });
    });
    expect(useTimelineStore.getState().items.map((i) => i.item_id)).toEqual(["5.0"]);

    act(() => {

      const history = [0, 1, 2, 3, 4].map((i) =>
        item({ item_id: `${i}.0`, kind: "agent_chat", payload: `msg ${i}` }),
      );
      useTimelineStore.getState().reloadSnapshot(history, 5, false);
    });
    const ids = useTimelineStore.getState().items.map((i) => i.item_id);
    expect(ids).toEqual(["0.0", "1.0", "2.0", "3.0", "4.0", "5.0"]);
  });
});

describe("compact_done hard reset (incremental design)", () => {
  it("active-thread compact_done KEEPS the pre-compact items visible and arms the reset window", () => {

    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          item({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" }),
          item({ item_id: "91.0", kind: "agent_chat", payload: "pre-compact reply" }),
        ],
        true,
      );
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "compact_done",
        agent_id: 1,
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "90.0", "91.0"]);
    expect(s.streamingIds.size).toBe(0);
    expect(s.resetPending).toBe(true);

    expect(s.compactReplaceSeq).toBe(0);
    expect(s.compactReplaceAgent).toBeNull();
  });

  it("an EMPTY timeline_snapshot inside the reset window is ignored (does not blank the panel)", () => {

    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [item({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })],
        false,
      );
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 0,
        items: [],
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["90.0"]);
    expect(s.resetPending).toBe(true);
  });

  it("the first post-compact snapshot replaces items wholesale and clears the reset window", () => {
    act(() => {
      useTimelineStore.getState().switchThread(1, [item({ item_id: "90.0", payload: "old" })], false);
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 3,
        items: [
          item({ item_id: "1.0", kind: "system_marker", payload: "[summary]" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "post-compact" }),
        ],
      });
    });
    const s = useTimelineStore.getState();

    expect(s.items.map((i) => i.item_id)).toEqual(["1.0", "2.0"]);
    expect(s.resetPending).toBe(false);

    expect(s.compactReplaceSeq).toBe(1);
    expect(s.compactReplaceAgent).toBe(1);
  });

  it("a GET reload inside the reset window is dropped (checkpoint may lag pre-compact); hasMoreOlder still refreshes", () => {
    act(() => {
      useTimelineStore.getState().switchThread(1, [item({ item_id: "90.0", payload: "old" })], true);
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    act(() => {
      useTimelineStore.getState().reloadSnapshot(
        [item({ item_id: "90.0", kind: "inbound_chat", payload: "LAGGING pre-compact" })],
        95,
        true,
      );
    });
    const s = useTimelineStore.getState();

    expect(s.items.map((i) => i.item_id)).toEqual(["90.0"]);
    expect(s.hasMoreOlder).toBe(true);
    expect(s.resetPending).toBe(true);
  });

  it("SSE reconnect clears the reset window so a GET can apply again", () => {
    act(() => {
      useTimelineStore.getState().switchThread(1, [item({ item_id: "90.0", payload: "old" })], false);
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    expect(useTimelineStore.getState().resetPending).toBe(true);
    act(() => {
      useTimelineStore.getState().processConnectionEvent({ type: "open" });
    });
    expect(useTimelineStore.getState().resetPending).toBe(false);
  });
});

describe("compact crossed unseen (SSE-gap heal)", () => {

  const ENVELOPE_CREATED = "2026-08-30T04:00:00.123456+00:00";
  const envelope = (overrides: Partial<BackendTimelineItem> = {}): BackendTimelineItem =>
    item({
      item_id: "1.0",
      kind: "inbound_compact_request",
      payload: "Compact request 2026-08-30 12:00:00:\n\nsummary text",
      created_at: ENVELOPE_CREATED,
      ...overrides,
    });

  it("reloadSnapshot replaces wholesale when the fetched snapshot crossed a compact the local items never folded", () => {
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [
          item({ item_id: "1.0", kind: "inbound_chat", payload: "pre-compact" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "pre-compact reply" }),
        ],
        false,
      );
    });
    act(() => {
      useTimelineStore.getState().reloadSnapshot(
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          envelope(),
          item({ item_id: "2.0", kind: "agent_chat", payload: "narration" }),
        ],
        3,
        true,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "1.0", "2.0"]);
    expect(s.items.some((i) => i.payload === "pre-compact reply")).toBe(false);
    expect(s.hasMoreOlder).toBe(true);
    expect(s.compactReplaceSeq).toBe(1);
    expect(s.compactReplaceAgent).toBe(1);
  });

  it("reloadSnapshot inside an armed reset window replaces when the GET crossed the compact (post-compact GET)", () => {
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [item({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })],
        true,
      );
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 1 });
    });
    act(() => {
      useTimelineStore.getState().reloadSnapshot(
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          envelope(),
        ],
        2,
        false,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "1.0"]);
    expect(s.resetPending).toBe(false);
  });

  it("reloadSnapshot keeps the normal keep-all merge when the fetched envelope is the same compact (payload ts differs, identity holds)", () => {

    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          envelope(),
          item({ item_id: "2.0", kind: "agent_chat", payload: "narration" }),
          item({ item_id: "3.0", kind: "agent_chat", payload: "newer SSE-folded reply" }),
        ],
        false,
      );
    });
    act(() => {
      useTimelineStore.getState().reloadSnapshot(
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          envelope({ payload: "Compact request 2026-08-30 12:00:05:\n\nsummary text" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "narration" }),
        ],
        3,
        false,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "1.0", "2.0", "3.0"]);
  });

  it("same slot + same kind but a different created_at (a newer compact reused the wiped slot) still crosses", () => {
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          envelope(),
          item({ item_id: "2.0", kind: "agent_chat", payload: "post-compact-1 turn" }),
        ],
        false,
      );
    });
    act(() => {
      useTimelineStore.getState().reloadSnapshot(
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          envelope({ created_at: "2026-08-30T04:05:00.654321+00:00" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "narration 2" }),
        ],
        3,
        false,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "1.0", "2.0"]);
    expect(s.items.some((i) => i.payload === "post-compact-1 turn")).toBe(false);
  });

  it("a full-window post-compact snapshot replaces the ACTIVE thread even when compact_done was lost", () => {
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [
          item({ item_id: "1.0", kind: "inbound_chat", payload: "pre-compact" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "pre-compact reply" }),
        ],
        false,
      );
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 3,
        items: [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          envelope(),
          item({ item_id: "2.0", kind: "agent_chat", payload: "narration" }),
        ],
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "1.0", "2.0"]);
    expect(s.resetPending).toBe(false);

    expect(s.compactReplaceSeq).toBe(1);
    expect(s.compactReplaceAgent).toBe(1);
  });
});
