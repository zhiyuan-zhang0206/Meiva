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
    compactHistoryPages: 1,
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

describe("processConnectionEvent", () => {
  it("closed → connectionState='closed' + turnActive=false + streamingCode=false", () => {
    act(() => {
      useTimelineStore.setState({ turnActive: true, streamingCode: true });
      useTimelineStore.getState().processConnectionEvent({ type: "closed" });
    });
    const s = useTimelineStore.getState();
    expect(s.connectionState).toBe("closed");
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);
  });

  it("closed marks partial items as interrupted, non-partial items unchanged", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", kind: "agent_chat", payload: "done", partial: false }),
      item({ item_id: "2.0", kind: "agent_reasoning", payload: "...", partial: true }),
      item({ item_id: "3.0", kind: "code_output", payload: "out" }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "closed" });
    });
    const after = useTimelineStore.getState().items;
    expect(after[0].interrupted).toBeFalsy();
    expect(after[1].interrupted).toBe(true);
    expect(after[2].interrupted).toBeFalsy();

    expect(after[1].partial).toBe(true);
  });

  it("closed does not re-mark partials that are already interrupted (but still ok to produce a new object)", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "x", partial: true, interrupted: true }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "closed" });
    });
    const after = useTimelineStore.getState().items;

    expect(after[0]).toBe(items[0]);
  });

  it("closed with no partials → items reference unchanged (some=false short-circuit)", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "a" }),
      item({ item_id: "2.0", payload: "b" }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "closed" });
    });
    expect(useTimelineStore.getState().items).toBe(items);
  });

  it("open → connectionState='open' + clears interrupted flag", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "a", partial: true, interrupted: true }),
      item({ item_id: "2.0", payload: "b", partial: false }),
    ];
    act(() => {
      useTimelineStore.setState({ items, connectionState: "closed" });
      useTimelineStore.getState().processConnectionEvent({ type: "open" });
    });
    const after = useTimelineStore.getState().items;
    expect(useTimelineStore.getState().connectionState).toBe("open");
    expect(after[0].interrupted).toBe(false);
    expect(after[0].partial).toBe(true);
    expect(after[1]).toBe(items[1]);
  });

  it("open with no interrupted items → items ref unchanged (some=false short-circuit)", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "a", partial: true }),
      item({ item_id: "2.0", payload: "b" }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "open" });
    });
    expect(useTimelineStore.getState().items).toBe(items);
  });

  it("reconnecting → connectionState='reconnecting', items unchanged, lifecycle flags untouched", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "x", partial: true }),
    ];
    act(() => {
      useTimelineStore.setState({ items, turnActive: true, streamingCode: true });
      useTimelineStore.getState().processConnectionEvent({ type: "reconnecting" });
    });
    const s = useTimelineStore.getState();
    expect(s.connectionState).toBe("reconnecting");
    expect(s.items).toBe(items);
    expect(s.turnActive).toBe(true);
    expect(s.streamingCode).toBe(true);
  });

  it("reconnecting keeps interrupted items (does not trigger open-branch clear)", () => {
    const items: BackendTimelineItem[] = [
      item({ item_id: "1.0", payload: "x", partial: true, interrupted: true }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().processConnectionEvent({ type: "reconnecting" });
    });

    expect(useTimelineStore.getState().items[0].interrupted).toBe(true);
    expect(useTimelineStore.getState().items).toBe(items);
  });
});

describe("clearPartialFlags", () => {
  it("mixed partial/non-partial set: only partial gets cleared, non-partial item ref unchanged (some !== every)", () => {
    const a = item({ item_id: "1.0", payload: "committed", partial: false });
    const b = item({ item_id: "2.0", payload: "still loading", partial: true });
    const c = item({ item_id: "3.0", payload: "another commit" });
    act(() => {
      useTimelineStore.setState({ items: [a, b, c] });
      useTimelineStore.getState().clearPartialFlags();
    });
    const after = useTimelineStore.getState().items;
    expect(after).toHaveLength(3);
    expect(after[0]).toBe(a);
    expect(after[1].partial).toBe(false);
    expect(after[1].payload).toBe("still loading");
    expect(after[2]).toBe(c);
  });

  it("no partials → items ref unchanged (some short-circuit)", () => {
    const items = [item({ item_id: "1.0", payload: "a" })];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().clearPartialFlags();
    });
    expect(useTimelineStore.getState().items).toBe(items);
  });

  it("all partial → all flipped to false (verifies some=true enters the path)", () => {
    const items = [
      item({ item_id: "1.0", payload: "a", partial: true }),
      item({ item_id: "2.0", payload: "b", partial: true }),
    ];
    act(() => {
      useTimelineStore.setState({ items });
      useTimelineStore.getState().clearPartialFlags();
    });
    const after = useTimelineStore.getState().items;
    expect(after.every((i) => i.partial === false)).toBe(true);
  });
});

describe("switchThread defaults", () => {

  it("cold switch (cached=null) clears items / flips all lifecycle flags / zeros tokens / sets activeThreadId", () => {

    act(() => {
      useTimelineStore.setState({
        items: [item({ payload: "stale" })],
        turnActive: true,
        streamingCode: true,
        connectionState: "closed",
        tokenUsage: 999,
        reasoningTokens: 77,
        maxContextTokens: 200000,
        activeThreadId: 7,
      });
      useTimelineStore.getState().switchThread(99, null, false);
    });
    const s = useTimelineStore.getState();
    expect(s.items).toEqual([]);
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);

    expect(s.connectionState).toBe("closed");
    expect(s.tokenUsage).toBe(0);
    expect(s.reasoningTokens).toBe(0);
    expect(s.maxContextTokens).toBe(0);
    expect(s.activeThreadId).toBe(99);
  });

  it("hot switch (cached items) installs the cached items + sets activeThreadId + zeros tokens (useTokenUsage restores)", () => {
    act(() => {
      useTimelineStore.setState({
        items: [item({ payload: "stale" })],
        turnActive: true,
        streamingCode: true,
        connectionState: "closed",
        tokenUsage: 555,
      });
      const cachedItems = [item({ payload: "restored" })];
      useTimelineStore.getState().switchThread(8, cachedItems, false);
    });
    const s = useTimelineStore.getState();
    expect(s.items).toHaveLength(1);
    expect(s.items[0].payload).toBe("restored");
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);

    expect(s.connectionState).toBe("closed");
    expect(s.activeThreadId).toBe(8);

    expect(s.tokenUsage).toBe(0);
  });

  it("switchThread preserves the connection state (Task #1051 regression: it used to stamp 'open', hiding a real disconnect)", () => {
    act(() => {
      useTimelineStore.setState({ connectionState: "closed" });
      useTimelineStore.getState().switchThread(99, null, false);
    });
    expect(useTimelineStore.getState().connectionState).toBe("closed");

    act(() => {
      useTimelineStore.setState({ connectionState: "open" });
      useTimelineStore.getState().switchThread(97, null, false);
    });
    expect(useTimelineStore.getState().connectionState).toBe("open");
  });

  it("hot switch carries hasMoreOlder from the cache; cold switch forces it false", () => {
    act(() => {
      useTimelineStore.getState().switchThread(3, [item({ payload: "hot" })], true);
    });
    expect(useTimelineStore.getState().hasMoreOlder).toBe(true);
    act(() => {

      useTimelineStore.getState().switchThread(4, null, true);
    });
    expect(useTimelineStore.getState().hasMoreOlder).toBe(false);
  });
});
