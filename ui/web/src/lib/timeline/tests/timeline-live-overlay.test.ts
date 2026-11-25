import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { mergeSnapshotWithStreaming } from "../timeline";
import { useTimelineStore } from "../timeline-store";
import type { BackendTimelineItem, SystemEvent } from "../../contracts/types";

const AGENT = 42;
const TEXT_LIVE = "live.t1.msg_a:0";
const THINK_LIVE = "live.t1.msg_a:1";

function committed(itemId: string, overrides: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return {
    item_id: itemId,
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

function snapshot(
  items: BackendTimelineItem[],
  retires?: string[],
  msg_count = 10,
): SystemEvent {
  return {
    role: "timeline_snapshot",
    agent_id: AGENT,
    items,
    msg_count,
    ...(retires === undefined ? {} : { retires }),
  };
}

const store = () => useTimelineStore.getState();
const ids = () => store().items.map((it) => it.item_id);
const send = (...events: SystemEvent[]) =>
  act(() => {
    for (const ev of events) store().processSseEvent(ev);
  });

beforeEach(() => {
  useTimelineStore.setState({
    activeThreadId: AGENT,
    items: [],
    compactBuffer: null,
    compactEpoch: 0,
    streamingIds: new Set(),
    streamingCode: false,
    turnActive: false,
    connectionState: "open",
    hasMoreOlder: false,
    loadingOlder: false,
    olderFetchCount: 0,
    liveCompact: null,
    compactReplaceSeq: 0,
    compactReplaceAgent: null,
  });
});

describe("overlay rows stream like native rows", () => {
  it("builds the text from deltas and shows the visibility-stripped payload while writing", () => {
    send(
      { role: "chat_start", agent_id: AGENT, item_id: TEXT_LIVE, explicit_visibility: true },
      { role: "chat_delta", agent_id: AGENT, item_id: TEXT_LIVE, content: "MEIVA_DISPLAY_TO", explicit_visibility: true },
      { role: "chat_delta", agent_id: AGENT, item_id: TEXT_LIVE, content: "_USER\nHel", explicit_visibility: true },
      { role: "chat_delta", agent_id: AGENT, item_id: TEXT_LIVE, content: "lo", explicit_visibility: true },
    );
    expect(store().turnActive).toBe(true);
    expect(store().items).toHaveLength(1);
    expect(store().items[0]).toMatchObject({ item_id: TEXT_LIVE, payload: "Hello", user_facing: true });
  });

  it("starts a reasoning clock on a live reasoning row", () => {
    send({ role: "reasoning_start", agent_id: AGENT, item_id: THINK_LIVE });
    expect(store().items[0].reasoningStartedAt).toBeDefined();
    expect(store().items[0].reasoning_ms).toBeUndefined();
  });
});

describe("a snapshot retires the overlay it supersedes", () => {
  it("swaps the live row for the committed row in one step: never both, never neither", () => {
    send(
      { role: "chat_start", agent_id: AGENT, item_id: TEXT_LIVE },
      { role: "chat_delta", agent_id: AGENT, item_id: TEXT_LIVE, content: "Hello world" },
    );
    const seen: string[][] = [];
    const unsubscribe = useTimelineStore.subscribe((s) => seen.push(s.items.map((it) => it.item_id)));
    send(snapshot([committed("h900.0", { payload: "Hello world" })], [TEXT_LIVE]));
    unsubscribe();

    expect(ids()).toEqual(["h900.0"]);
    expect(seen.length).toBeGreaterThan(0);
    for (const frame of seen) {
      expect(frame.filter((id) => id === "h900.0" || id === TEXT_LIVE)).toHaveLength(1);
    }
    expect(store().streamingIds.has(TEXT_LIVE)).toBe(false);
  });

  it("retires only the named block; the block still being written stays and sorts last", () => {
    send(
      { role: "reasoning_start", agent_id: AGENT, item_id: THINK_LIVE },
      { role: "reasoning_delta", agent_id: AGENT, item_id: THINK_LIVE, content: "hmm" },
      { role: "chat_start", agent_id: AGENT, item_id: TEXT_LIVE },
      { role: "chat_delta", agent_id: AGENT, item_id: TEXT_LIVE, content: "Answer so far" },
    );
    send(snapshot([committed("h900.0", { kind: "agent_reasoning", payload: "hmm" })], [THINK_LIVE]));
    expect(ids()).toEqual(["h900.0", TEXT_LIVE]);
    expect(store().streamingIds.has(TEXT_LIVE)).toBe(true);
    expect(store().items[1].payload).toBe("Answer so far");
  });

  it("keeps a live row after every committed row, however the committed ids sort", () => {
    const history = [
      committed("3.0", { kind: "inbound_chat", source: "user", payload: "question" }),
      committed("h120.0", { payload: "earlier answer", created_at: "2026-01-01T00:00:01Z" }),
    ];
    send(snapshot(history));
    send({ role: "chat_start", agent_id: AGENT, item_id: TEXT_LIVE });
    send(snapshot([committed("h200.0", { payload: "next", created_at: "2026-01-01T00:00:02Z" })]));
    expect(ids()).toEqual(["3.0", "h120.0", "h200.0", TEXT_LIVE]);
  });

  it("an empty snapshot that only retires removes the live rows, clocks and tracking (turn end)", () => {
    send(
      { role: "reasoning_start", agent_id: AGENT, item_id: THINK_LIVE },
      { role: "chat_start", agent_id: AGENT, item_id: TEXT_LIVE },
      { role: "chat_delta", agent_id: AGENT, item_id: TEXT_LIVE, content: "cut off mid-sen" },
    );
    send(snapshot([], [THINK_LIVE, TEXT_LIVE]));
    expect(store().items).toEqual([]);
    expect(store().streamingIds.size).toBe(0);
  });

  it("removes a retired id that the record already served, e.g. a GET that raced the retire", () => {
    send(
      { role: "chat_start", agent_id: AGENT, item_id: TEXT_LIVE },
      { role: "chat_delta", agent_id: AGENT, item_id: TEXT_LIVE, content: "Hi" },
    );
    act(() => {
      store().reloadSnapshot([committed("h900.0", { payload: "Hi" })], 10, false);
    });
    expect(ids().sort()).toEqual(["h900.0", TEXT_LIVE].sort());
    send(snapshot([], [TEXT_LIVE]));
    expect(ids()).toEqual(["h900.0"]);
    expect(store().items[0].payload).toBe("Hi");
  });

  it("leaves everything alone when a retired id is not on screen", () => {
    send(snapshot([committed("h900.0", { payload: "Hi" })]));
    const before = store().items;
    send(snapshot([], [TEXT_LIVE]));
    expect(store().items).toBe(before);
  });

  it("without retires an overlay row survives snapshots (the id is not a record position)", () => {
    send({ role: "chat_start", agent_id: AGENT, item_id: TEXT_LIVE });
    send(snapshot([committed("h900.0", { payload: "other block" })]));
    expect(ids()).toEqual(["h900.0", TEXT_LIVE]);
  });

  it("an interrupt drops the half-written text and leaves committed history", () => {
    send(snapshot([committed("h100.0", { payload: "kept" })]));
    send(
      { role: "chat_start", agent_id: AGENT, item_id: TEXT_LIVE },
      { role: "chat_delta", agent_id: AGENT, item_id: TEXT_LIVE, content: "half-wri" },
    );
    send(snapshot([], [TEXT_LIVE]));
    expect(ids()).toEqual(["h100.0"]);
  });
});

describe("mergeSnapshotWithStreaming retires", () => {
  it("removes a retired id even while the stream still tracks it", () => {
    const live = committed(TEXT_LIVE, { partial: true, payload: "x" });
    const merged = mergeSnapshotWithStreaming([live], [], 5, new Set([TEXT_LIVE]), [TEXT_LIVE]);
    expect(merged).toEqual([]);
  });

  it("returns prev unchanged when retires names nothing present", () => {
    const prev = [committed("h1.0", { payload: "a" })];
    expect(mergeSnapshotWithStreaming(prev, [], 5, new Set(), ["live.t9.x"])).toBe(prev);
  });
});
