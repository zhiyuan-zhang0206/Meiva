import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useTimelineStore } from "../timeline-store";
import type { BackendTimelineItem, SystemEvent } from "../../contracts/types";

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

describe("system-prompt snapshot identity", () => {
  it("a snapshot without the system-prompt item keeps the prev 0.0 object verbatim (no 128KB re-alloc)", () => {

    const sysPrompt = item({ item_id: "0.0", kind: "system_prompt", payload: "same" });
    act(() => {
      useTimelineStore.getState().switchThread(
        1,
        [sysPrompt, item({ item_id: "1.0", kind: "agent_chat", payload: "a" })],
        false,
      );
    });
    const before = useTimelineStore.getState().items[0];
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 3,
        items: [
          item({ item_id: "1.0", kind: "agent_chat", payload: "a" }),
          item({ item_id: "2.0", kind: "agent_chat", payload: "b" }),
        ],
      });
    });
    const after = useTimelineStore.getState().items;
    expect(after[0]).toBe(before);
    expect(after.map((i) => i.item_id)).toEqual(["0.0", "1.0", "2.0"]);
  });

  it("a snapshot that DOES carry a changed 0.0 (full-window paths: spawn / compact / restart) replaces it", () => {
    const sysPrompt = item({ item_id: "0.0", kind: "system_prompt", payload: "old" });
    act(() => {
      useTimelineStore.getState().switchThread(1, [sysPrompt], false);
    });
    const fresh = item({ item_id: "0.0", kind: "system_prompt", payload: "new" });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 1,
        items: [fresh],
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items[0]).toBe(fresh);
    expect(s.items[0]?.payload).toBe("new");
  });
});

describe("spawn scenario: first snapshot carries 0.0 (#615)", () => {
  it("cold thread (empty GET at spawn) gets the system-prompt item from the first SSE snapshot", () => {

    act(() => {
      useTimelineStore.getState().switchThread(1, [], false);
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 1,
        msg_count: 1,
        items: [
          item({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
        ],
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["0.0"]);
    expect(s.items[0].payload).toBe("PROMPT");
  });
});

describe("processSseEventBatch — frame-level folding", () => {
  it("folds a whole frame's deltas in ONE store notification, in arrival order", () => {
    const listener = vi.fn();
    const unsub = useTimelineStore.subscribe(listener);
    act(() => {
      useTimelineStore.getState().processSseEventBatch([
        { role: "chat_start", agent_id: 42, item_id: "1.0" },
        { role: "chat_delta", agent_id: 42, item_id: "1.0", content: "he" },
        { role: "chat_delta", agent_id: 42, item_id: "1.0", content: "llo" },
        { role: "llm_done", agent_id: 42 },
      ]);
    });
    unsub();
    const s = useTimelineStore.getState();

    expect(listener).toHaveBeenCalledTimes(1);
    expect(s.items).toHaveLength(1);
    expect(s.items[0].item_id).toBe("1.0");
    expect(s.items[0].payload).toBe("hello");
    expect(s.turnActive).toBe(false);
  });

  it("one set() per frame — subscriber notified once for a 4-event burst", () => {
    const listener = vi.fn();
    const unsub = useTimelineStore.subscribe(listener);
    act(() => {
      useTimelineStore.getState().processSseEventBatch([
        { role: "code_start", agent_id: 42, item_id: "2.0" },
        { role: "code_delta", agent_id: 42, item_id: "2.0", content: "x" },
        { role: "code_delta", agent_id: 42, item_id: "2.0", content: "y" },
        { role: "exec_start", agent_id: 42, item_id: "2.1" },
      ]);
    });
    unsub();
    expect(listener).toHaveBeenCalledTimes(1);
    const s = useTimelineStore.getState();
    expect(s.items.map((i) => i.item_id)).toEqual(["2.0", "2.1"]);
  });

  it("empty batch and no-op batches never notify", () => {
    const listener = vi.fn();
    const unsub = useTimelineStore.subscribe(listener);
    act(() => {
      useTimelineStore.getState().processSseEventBatch([]);

      useTimelineStore.getState().processSseEventBatch([
        { role: "agent_spawned", agent_id: 99 },
        { role: "agent_updated", agent_id: 99 },
      ]);
    });
    unsub();
    expect(listener).not.toHaveBeenCalled();
  });

  it("token_usage in a batch applies like the per-event path (last wins)", () => {
    act(() => {
      useTimelineStore.getState().processSseEventBatch([
        { role: "token_usage", agent_id: 42, input_tokens: 100, output_tokens: 1 },
        { role: "token_usage", agent_id: 42, input_tokens: 222, output_tokens: 2 },

        { role: "token_usage", agent_id: 7, input_tokens: 999, output_tokens: 9 },
      ]);
    });
    const s = useTimelineStore.getState();
    expect(s.tokenUsage).toBe(222);
    expect(s.reasoningTokens).toBe(0);
  });

  it("a batch matches sequential per-event application (no divergence)", () => {

    act(() => {
      useTimelineStore.getState().switchThread(42, [], false);
      useTimelineStore.getState().switchThread(1, [], false);
    });
    const frame = (): SystemEvent[] => [
      { role: "code_start", agent_id: 1, item_id: "3.0" },
      { role: "code_delta", agent_id: 1, item_id: "3.0", content: "pri" },
      { role: "code_delta", agent_id: 1, item_id: "3.0", content: "nt" },
      { role: "chat_delta", agent_id: 42, item_id: "9.0", content: "bg" },
      { role: "exec_output", agent_id: 1, item_id: "3.0", content: "done" },
    ];

    act(() => {
      useTimelineStore.getState().processSseEventBatch(frame());
    });
    const afterBatch = {
      items: useTimelineStore.getState().items,
      streamingCode: useTimelineStore.getState().streamingCode,
      turnActive: useTimelineStore.getState().turnActive,
    };

    resetStore();
    useTimelineStore.setState({ resetPending: false });
    act(() => {
      useTimelineStore.getState().switchThread(42, [], false);
      useTimelineStore.getState().switchThread(1, [], false);
      for (const ev of frame()) {
        useTimelineStore.getState().processSseEvent(ev);
      }
    });

    const strip = (list: BackendTimelineItem[]) =>
      list.map((i) => ({ item_id: i.item_id, payload: i.payload }));
    expect(strip(useTimelineStore.getState().items)).toEqual(strip(afterBatch.items));
    expect(useTimelineStore.getState().streamingCode).toBe(afterBatch.streamingCode);
    expect(useTimelineStore.getState().turnActive).toBe(afterBatch.turnActive);

  });
});

describe("liveCompact — compact_started / compact_finished (task #3324)", () => {
  const started = (over?: Record<string, unknown>): SystemEvent =>
    ({
      agent_id: 42,
      role: "compact_started",
      compact_id: "c1",
      started_at: "2026-09-14T03:00:00+00:00",
      mode: "request",
      ...over,
    }) as unknown as SystemEvent;
  const finished = (over?: Record<string, unknown>): SystemEvent =>
    ({
      agent_id: 42,
      role: "compact_finished",
      compact_id: "c1",
      status: "success",
      finished_at: "2026-09-14T03:00:24+00:00",
      ...over,
    }) as unknown as SystemEvent;

  it("started opens the entry; finished pairs by compact_id and settles", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent(started());
    });
    expect(useTimelineStore.getState().liveCompact).toEqual({
      compactId: "c1",
      startedAt: "2026-09-14T03:00:00+00:00",
      mode: "request",
      status: null,
      finishedAt: null,
    });
    act(() => {
      useTimelineStore.getState().processSseEvent(finished());
    });
    expect(useTimelineStore.getState().liveCompact).toEqual({
      compactId: "c1",
      startedAt: "2026-09-14T03:00:00+00:00",
      mode: "request",
      status: "success",
      finishedAt: "2026-09-14T03:00:24+00:00",
    });
  });

  it("drops a terminal for a superseded run (compact_id mismatch)", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent(started());
      useTimelineStore.getState().processSseEvent(started({ compact_id: "c2" }));
      useTimelineStore.getState().processSseEvent(
        finished({ compact_id: "c1", status: "replaced" }),
      );
    });
    expect(useTimelineStore.getState().liveCompact?.compactId).toBe("c2");
    expect(useTimelineStore.getState().liveCompact?.status).toBeNull();
  });

  it("records an entry-less terminal (start lost in an SSE gap)", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent(finished({ status: "failure" }));
    });
    expect(useTimelineStore.getState().liveCompact).toEqual({
      compactId: "c1",
      startedAt: null,
      mode: null,
      status: "failure",
      finishedAt: "2026-09-14T03:00:24+00:00",
    });
  });

  it("retires when the run's summary item lands in a snapshot", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent(started());
      useTimelineStore.getState().processSseEvent(finished());
    });
    const summary = item({
      kind: "inbound_compact_summary",
      item_id: "0.0",
      compact_id: "c1",
    });
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 42,
        items: [summary],
      } as unknown as SystemEvent);
    });
    expect(useTimelineStore.getState().liveCompact).toBeNull();
    expect(useTimelineStore.getState().items).toEqual([summary]);
  });

  it("clears on thread switch", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent(started());
      useTimelineStore.getState().switchThread(7, [], false);
    });
    expect(useTimelineStore.getState().liveCompact).toBeNull();
  });
});

describe("selected-only live state", () => {
  it("ignores old detail and compact frames without retaining inactive buckets", () => {
    const store = useTimelineStore.getState();
    store.switchThread(1, [item({ item_id: "1.0", payload: "A" })], false);
    store.switchThread(2, [item({ item_id: "1.0", payload: "B" })], false);
    store.processSseEventBatch([
      { role: "chat_delta", agent_id: 1, item_id: "1.0", content: "late" },
      { role: "compact_done", agent_id: 1 },
    ]);
    expect(useTimelineStore.getState().items.map((entry) => entry.payload)).toEqual(["B"]);
    expect(useTimelineStore.getState().resetPending).toBe(false);
    for (let id = 3; id < 1000; id += 1) store.switchThread(id, [], false);
    store.switchThread(1, null, false);
    expect(useTimelineStore.getState().items).toEqual([]);
    expect(Object.keys(useTimelineStore.getState())).not.toContain("threads");
  });
});

describe("retained compact reading windows", () => {
  it("restores the old buffer under the new selection epoch", () => {
    const buffer = { threadId: 42, epoch: 17, rows: [], oldestRealByRank: new Map<number, string>(), tailReady: true };
    useTimelineStore.getState().switchThread(42, [item({ item_id: "1.0" })], true, buffer);
    const view = useTimelineStore.getState();
    expect(view.compactBuffer).toEqual({ ...buffer, epoch: view.compactEpoch });
    expect(buffer.epoch).toBe(17);
    expect(() => useTimelineStore.getState().switchThread(43, [], true, buffer)).toThrow("must belong");
    expect(useTimelineStore.getState().activeThreadId).toBe(42);
  });
});

describe("live clock freezing and settleTurn", () => {
  it("freezes execStartedAt into execElapsedMs on exec_output", () => {
    const store = useTimelineStore.getState();
    store.switchThread(42, [], false);
    store.processSseEvent({ role: "exec_start", agent_id: 42, item_id: "2.0" });
    expect(useTimelineStore.getState().items[0].execStartedAt).toEqual(expect.any(Number));
    store.processSseEvent({ role: "exec_output", agent_id: 42, item_id: "2.0", content: "ok" });
    const out = useTimelineStore.getState().items[0];
    expect(out.execStartedAt).toBeUndefined();
    expect(out.execElapsedMs).toEqual(expect.any(Number));
    expect(useTimelineStore.getState().turnActive).toBe(false);
  });

  it("keeps an earlier concurrent execStartedAt live when a second tool starts and finishes", () => {
    const store = useTimelineStore.getState();
    store.switchThread(42, [], false);
    store.processSseEvent({ role: "exec_start", agent_id: 42, item_id: "7.0" });
    const anchor = useTimelineStore.getState().items[0].execStartedAt;
    expect(anchor).toEqual(expect.any(Number));
    store.processSseEvent({ role: "code_start", agent_id: 42, item_id: "8.0" });
    store.processSseEvent({ role: "exec_start", agent_id: 42, item_id: "9.0" });
    store.processSseEvent({ role: "exec_output", agent_id: 42, item_id: "9.0", content: "fast" });
    const state = useTimelineStore.getState();
    const firstExec = state.items.find((it) => it.item_id === "7.0");
    expect(firstExec?.execStartedAt).toBe(anchor);
    expect(state.turnActive).toBe(true);
  });

  it("settleTurn freezes live clocks, drops empty exec placeholders, and clears turnActive", () => {
    const store = useTimelineStore.getState();
    store.switchThread(42, [], false);
    store.processSseEvent({ role: "code_start", agent_id: 42, item_id: "1.0" });
    store.processSseEvent({ role: "code_delta", agent_id: 42, item_id: "1.0", content: "x=1" });
    store.processSseEvent({ role: "exec_start", agent_id: 42, item_id: "2.0" });
    expect(useTimelineStore.getState().turnActive).toBe(true);
    store.settleTurn();
    const settled = useTimelineStore.getState();
    expect(settled.turnActive).toBe(false);
    expect(settled.streamingCode).toBe(false);
    expect(settled.streamingIds.size).toBe(0);
    expect(settled.items.map((it) => it.item_id)).toEqual(["1.0"]);
    expect(settled.items[0].codeStartedAt).toBeUndefined();
    expect(settled.items[0].codeElapsedMs).toEqual(expect.any(Number));
  });
});
