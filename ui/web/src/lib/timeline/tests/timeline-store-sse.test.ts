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

describe("processSseEvent: cancelled", () => {

  it("drops the streamed generation bubble by id, keeps committed items, flips flags", () => {
    const committed = item({ item_id: "2.0", kind: "agent_chat", payload: "earlier reply" });
    act(() => {
      useTimelineStore.setState({ items: [committed], streamingIds: new Set(), turnActive: true });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "chat_start", agent_id: 42, item_id: "3.0" });
      ps({ role: "chat_delta", agent_id: 42, item_id: "3.0", content: "half-wri" });
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.1", content: "thinking" });
      ps({ role: "cancelled", agent_id: 42 });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["2.0"]);
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);
    expect(s.streamingIds.size).toBe(0);
  });

  it("keeps a partial code_output (exec interrupted) but clears empty exec placeholder", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;

      ps({ role: "exec_start", agent_id: 42, item_id: "5.0" });
      ps({ role: "exec_output_chunk", agent_id: 42, item_id: "5.0", content: "running…" });

      ps({ role: "exec_start", agent_id: 42, item_id: "6.0" });
      ps({ role: "cancelled", agent_id: 42 });
    });
    const items = useTimelineStore.getState().items;

    expect(items.map((i) => i.item_id)).toContain("5.0");

    expect(items.map((i) => i.item_id)).not.toContain("6.0");
  });

  it("a snapshot that commits a streamed id stops tracking it, so a later cancel keeps it", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "chat_start", agent_id: 42, item_id: "3.0" });

      ps({
        role: "timeline_snapshot",
        agent_id: 42,
        items: [item({ item_id: "3.0", kind: "agent_chat", payload: "committed" })],
        msg_count: 4,
      });
      ps({ role: "cancelled", agent_id: 42 });
    });

    expect(useTimelineStore.getState().items.map((i) => i.item_id)).toEqual(["3.0"]);
  });
});

describe("processSseEvent: live thinking clock freezes per block", () => {

  it("llm_done freezes an uncommitted reasoning item (anchor dropped, elapsed stamped)", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "reasoning_start", agent_id: 42, item_id: "3.0" });
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.0", content: "hmm" });
      ps({ role: "llm_done", agent_id: 42 });
    });
    const it30 = useTimelineStore.getState().items.find((i) => i.item_id === "3.0");
    expect(it30).toBeDefined();
    expect(it30?.reasoningStartedAt).toBeUndefined();
    expect(typeof it30?.reasoningElapsedMs).toBe("number");
  });

  it("chat_start freezes the live reasoning clock of the prior block", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "reasoning_start", agent_id: 42, item_id: "3.0" });
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.0", content: "plan" });
      ps({ role: "chat_start", agent_id: 42, item_id: "3.1" });
    });
    const reasoning = useTimelineStore.getState().items.find((i) => i.item_id === "3.0");
    expect(reasoning?.reasoningStartedAt).toBeUndefined();
    expect(typeof reasoning?.reasoningElapsedMs).toBe("number");
  });

  it("a second reasoning_start freezes the prior reasoning block only", () => {
    act(() => {
      useTimelineStore.setState({ items: [], streamingIds: new Set() });
      const ps = useTimelineStore.getState().processSseEvent;
      ps({ role: "reasoning_start", agent_id: 42, item_id: "3.0" });
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.0", content: "first" });
      ps({ role: "reasoning_start", agent_id: 42, item_id: "3.2" });
      ps({ role: "reasoning_delta", agent_id: 42, item_id: "3.2", content: "second" });
    });
    const items = useTimelineStore.getState().items;
    const first = items.find((i) => i.item_id === "3.0");
    const second = items.find((i) => i.item_id === "3.2");

    expect(first?.reasoningStartedAt).toBeUndefined();
    expect(typeof first?.reasoningElapsedMs).toBe("number");
    expect(typeof second?.reasoningStartedAt).toBe("number");
    expect(second?.reasoningElapsedMs).toBeUndefined();
  });

  it("a committed item (reasoning_ms set) keeps its fields through turn end", () => {
    act(() => {
      useTimelineStore.setState({
        items: [
          item({
            item_id: "3.0",
            kind: "agent_reasoning",
            payload: "done thinking",
            reasoning_ms: 2200,
          }),
        ],
        streamingIds: new Set(),
      });
      useTimelineStore.getState().processSseEvent({ role: "llm_done", agent_id: 42 });
    });
    const it30 = useTimelineStore.getState().items.find((i) => i.item_id === "3.0");
    expect(it30?.reasoning_ms).toBe(2200);
  });
});

describe("prependOlder / load-older flags", () => {
  it("beginLoadOlder sets loadingOlder; prependOlder prepends + sorts + clears it", () => {
    act(() => {
      useTimelineStore.setState({
        items: [
          item({ item_id: "5.0", kind: "inbound_chat", payload: "recent" }),
          item({ item_id: "6.0", payload: "newest" }),
        ],
        hasMoreOlder: true,
        loadingOlder: false,
      });
      useTimelineStore.getState().beginLoadOlder();
    });
    expect(useTimelineStore.getState().loadingOlder).toBe(true);

    act(() => {
      useTimelineStore.getState().prependOlder(
        [
          item({ item_id: "1.0", kind: "inbound_chat", payload: "oldest" }),
          item({ item_id: "2.0", payload: "old reply" }),
        ],
        false,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["1.0", "2.0", "5.0", "6.0"]);
    expect(s.hasMoreOlder).toBe(false);
    expect(s.loadingOlder).toBe(false);
  });

  it("prependOlder dedupes by item_id (a tail snapshot may already hold some)", () => {
    act(() => {
      useTimelineStore.setState({
        items: [item({ item_id: "5.0", kind: "inbound_chat", payload: "recent" })],
        hasMoreOlder: true,
        loadingOlder: true,
      });
      useTimelineStore.getState().prependOlder(
        [
          item({ item_id: "4.0", payload: "older" }),
          item({ item_id: "5.0", kind: "inbound_chat", payload: "dup recent" }),
        ],
        true,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["4.0", "5.0"]);
    expect(s.items.filter((i) => i.item_id === "5.0")).toHaveLength(1);
    expect(s.hasMoreOlder).toBe(true);
  });
  it("prependOlder stops paging when a page adds nothing but claims more (stuck standing-context cursor)", () => {

    act(() => {
      useTimelineStore.setState({
        items: [
          item({ item_id: "0.0", kind: "system_prompt", payload: "prompt" }),
          item({ item_id: "1.0", kind: "system_marker", source: "memory", payload: "m" }),
          item({ item_id: "7.0", kind: "inbound_chat", payload: "current" }),
        ],
        hasMoreOlder: true,
        loadingOlder: true,
      });
      useTimelineStore.getState().prependOlder(
        [
          item({ item_id: "0.0", kind: "system_prompt", payload: "prompt" }),
          item({ item_id: "1.0", kind: "system_marker", source: "memory", payload: "m" }),
        ],
        true,
      );
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0", "1.0", "7.0"]);
    expect(s.hasMoreOlder).toBe(false);
    expect(s.loadingOlder).toBe(false);
  });

  it("prepends history beyond the old 6000 cap without dropping items", () => {

    const current = Array.from({ length: 5_999 }, (_, index) =>
      item({ item_id: `${index + 1_000}.0`, payload: `current ${index}` }),
    );
    act(() => {
      useTimelineStore.setState({ items: current, hasMoreOlder: true, loadingOlder: true });
      useTimelineStore.getState().prependOlder(
        [
          item({ item_id: "s1.boundary.0.0", payload: "oldest kept" }),
          item({ item_id: "s1.boundary.1.0", payload: "nearest older kept" }),
        ],
        true,
      );
    });

    const state = useTimelineStore.getState();
    expect(state.items).toHaveLength(6_001);
    expect(state.items[0].item_id).toBe("s1.boundary.0.0");
    expect(state.items.some((entry) => entry.item_id === "s1.boundary.1.0")).toBe(true);
    expect(state.hasMoreOlder).toBe(true);
    expect(state.loadingOlder).toBe(false);
  });

  it("keeps paging when the retained timeline reaches exactly 6000 items", () => {
    const current = Array.from({ length: 5_999 }, (_, index) =>
      item({ item_id: `${index + 1_000}.0` }),
    );
    act(() => {
      useTimelineStore.setState({ items: current, hasMoreOlder: true, loadingOlder: true });
      useTimelineStore.getState().prependOlder(
        [item({ item_id: "s1.boundary.0.0", payload: "6000th retained item" })],
        true,
      );
    });

    const state = useTimelineStore.getState();
    expect(state.items).toHaveLength(6_000);
    expect(state.items[0].item_id).toBe("s1.boundary.0.0");

    expect(state.hasMoreOlder).toBe(true);
  });

  it("switchThread clears hasMoreOlder + loadingOlder (cold)", () => {
    act(() => {
      useTimelineStore.setState({ hasMoreOlder: true, loadingOlder: true });
      useTimelineStore.getState().switchThread(7, null, false);
    });
    const s = useTimelineStore.getState();
    expect(s.hasMoreOlder).toBe(false);
    expect(s.loadingOlder).toBe(false);
  });
});

describe("no per-thread item cap across live writers (task #1734)", () => {
  const fullTimeline = () =>
    Array.from({ length: 6_000 }, (_, index) =>
      item({ item_id: `${index + 1}.0`, payload: `item ${index + 1}` }),
    );

  it("keeps active SSE growth past 6000 items", () => {
    act(() => {
      useTimelineStore.setState({ items: fullTimeline(), hasMoreOlder: true });
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 42,
        item_id: "6001.0",
      });
    });

    const state = useTimelineStore.getState();
    expect(state.items).toHaveLength(6_001);
    expect(state.items[0].item_id).toBe("1.0");
    expect(state.items.at(-1)?.item_id).toBe("6001.0");
    expect(state.hasMoreOlder).toBe(true);
  });

  it("keeps an oversized switchThread seed in full", () => {
    const oversized = [
      item({ item_id: "0.0", payload: "oldest" }),
      ...fullTimeline(),
    ];

    act(() => {
      useTimelineStore.getState().switchThread(1, oversized, true);
    });

    const state = useTimelineStore.getState();
    expect(state.items).toHaveLength(6_001);
    expect(state.items[0].item_id).toBe("0.0");
    expect(state.items.at(-1)?.item_id).toBe("6000.0");
    expect(state.hasMoreOlder).toBe(true);
  });

  it("infinite paging: repeated prependOlder pages accumulate without a local cap", () => {

    const pages = 10;
    const pageSize = 1_000;
    act(() => {
      useTimelineStore.setState({ items: [], hasMoreOlder: true, loadingOlder: true });
      for (let page = 0; page < pages; page++) {
        const older = Array.from({ length: pageSize }, (_, index) =>
          item({
            item_id: `s1.boundary.${page * pageSize + index}.0`,
            payload: `page ${page}`,
          }),
        );
        useTimelineStore.getState().prependOlder(older, true);
      }
    });

    const state = useTimelineStore.getState();
    expect(state.items).toHaveLength(pages * pageSize);
    expect(state.items[0].item_id).toBe("s1.boundary.0.0");
    expect(state.items.at(-1)?.item_id).toBe(`s1.boundary.${pages * pageSize - 1}.0`);

    expect(state.hasMoreOlder).toBe(true);
    expect(state.loadingOlder).toBe(false);
  });

  it("infinite paging ends only when the backend reports has_more=false", () => {

    act(() => {
      useTimelineStore.setState({ items: [], hasMoreOlder: true, loadingOlder: true });
      useTimelineStore.getState().prependOlder(
        [item({ item_id: "s1.boundary.0.0", payload: "last segment tail" })],
        false,
      );
    });

    const state = useTimelineStore.getState();
    expect(state.items).toHaveLength(1);
    expect(state.hasMoreOlder).toBe(false);
    expect(state.loadingOlder).toBe(false);
  });
});

describe("selected event routing", () => {
  it("token_usage for an inactive thread is dropped; for the active thread it writes the top-level token fields", () => {
    const ps = useTimelineStore.getState().processSseEvent;
    act(() => {
      useTimelineStore.getState().switchThread(1, null, false);
      useTimelineStore.getState().switchThread(2, null, false);
      useTimelineStore.setState({ tokenUsage: 0, reasoningTokens: 0 });

      ps({ role: "token_usage", agent_id: 1, input_tokens: 111, output_tokens: 0 });
    });
    expect(useTimelineStore.getState().tokenUsage).toBe(0);

    act(() => {
      ps({ role: "token_usage", agent_id: 2, input_tokens: 222, output_tokens: 0, reasoning_tokens: 7 });
    });
    expect(useTimelineStore.getState().tokenUsage).toBe(222);
    expect(useTimelineStore.getState().reasoningTokens).toBe(7);
  });
});
