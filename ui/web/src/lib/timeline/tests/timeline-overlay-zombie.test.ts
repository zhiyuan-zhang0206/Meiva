import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { groupIntoTurns, summarizeTurn } from "@/components/timeline/model/runs";

import type { BackendTimelineItem, SystemEvent } from "../../contracts/types";
import { hasUnsettledClock } from "../timeline-plan";
import { useTimelineStore } from "../timeline-store";

const AGENT = 8926;
const FIRST_TURN_START = Date.parse("2026-11-26T03:46:42.200Z");
const SECOND_TURN_START = Date.parse("2026-11-26T04:08:04.719Z");
const SCREENSHOT_AT = Date.parse("2026-11-26T04:08:35.000Z");

function row(itemId: string, overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    item_id: itemId,
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-11-26T03:46:42.274Z",
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

function reasoning(itemId: string, createdAt: string, reasoningMs: number): BackendTimelineItem {
  return row(itemId, {
    kind: "agent_reasoning",
    payload: "thinking",
    created_at: createdAt,
    reasoning_ms: reasoningMs,
  });
}

function bash(itemId: string, createdAt: string): BackendTimelineItem {
  return row(itemId, { kind: "agent_code", payload: "ls", created_at: createdAt, code_elapsed_ms: 20 });
}

function snapshot(items: BackendTimelineItem[], retires?: string[]): SystemEvent {
  return {
    role: "timeline_snapshot",
    agent_id: AGENT,
    items,
    msg_count: 0,
    ...(retires === undefined ? {} : { retires }),
  };
}

const store = () => useTimelineStore.getState();
const liveIds = () => store().items.map((it) => it.item_id).filter((id) => id.startsWith("live."));
const send = (nowMs: number, ...events: SystemEvent[]) =>
  act(() => {
    for (const ev of events) store().processSseEvent(ev, nowMs);
  });

function liveWorkClockMs(nowMs: number): number {
  const groups = groupIntoTurns(store().items, { collapseTurns: true, liveIndex: null });
  const last = groups.findLast((g) => g.kind === "turn");
  if (last?.kind !== "turn") throw new Error("no work block");
  const { liveSegmentStartMs, priorActiveMs } = last.summary;
  return priorActiveMs + (liveSegmentStartMs === null ? 0 : nowMs - liveSegmentStartMs);
}

beforeEach(() => {
  useTimelineStore.setState({
    activeThreadId: AGENT,
    items: [],
    compactBuffer: null,
    compactEpoch: 0,
    streamingIds: new Set(),
    streamingCode: false,
    turnActive: false,
    turnRunning: false,
    connectionState: "open",
    hasMoreOlder: false,
    loadingOlder: false,
    olderFetchCount: 0,
    liveCompact: null,
    compactReplaceSeq: 0,
    compactReplaceAgent: null,
    resetPending: false,
  });
});

function firstTurnWithLostRetires(): void {
  send(
    FIRST_TURN_START,
    { role: "reasoning_start", agent_id: AGENT, item_id: "live.t1.msg_a:0" },
    { role: "reasoning_delta", agent_id: AGENT, item_id: "live.t1.msg_a:0", content: "short thought" },
  );
  send(
    FIRST_TURN_START + 19_000,
    { role: "reasoning_start", agent_id: AGENT, item_id: "live.t1.msg_c:0" },
    { role: "reasoning_delta", agent_id: AGENT, item_id: "live.t1.msg_c:0", content: "long thought" },
  );
  send(
    FIRST_TURN_START + 33_000,
    snapshot([
      row("h1706931.0", {
        kind: "inbound_chat",
        source: "user",
        payload: "continue",
        created_at: "2026-11-26T03:46:35.452Z",
      }),
      reasoning("h1709768.0", "2026-11-26T03:46:42.274Z", 2909),
      bash("h1711000.0", "2026-11-26T03:46:42.277Z"),
      reasoning("h1735249.0", "2026-11-26T03:47:15.865Z", 26642),
    ]),
  );
}

function secondTurn(): void {
  send(
    SECOND_TURN_START,
    snapshot([
      row("h1980000.0", {
        kind: "inbound_chat",
        source: "user",
        payload: "go on",
        created_at: "2026-11-26T04:08:04.719Z",
      }),
    ]),
  );
  send(
    Date.parse("2026-11-26T04:08:11.000Z"),
    snapshot([
      reasoning("h1980965.0", "2026-11-26T04:08:10.993Z", 2455),
      bash("h1984077.0", "2026-11-26T04:08:11.355Z"),
    ]),
  );
}

describe("an overlay whose retirement was lost does not outlive its turn", () => {
  it("settling the turn removes every overlay row, so no frozen Thought-for row is left behind", () => {
    firstTurnWithLostRetires();
    expect(liveIds()).toEqual(["live.t1.msg_a:0", "live.t1.msg_c:0"]);

    act(() => store().settleTurn(FIRST_TURN_START + 62_000));

    expect(liveIds()).toEqual([]);
    expect(store().items.some((it) => it.reasoningElapsedMs != null)).toBe(false);
  });

  it("an idle page that still shows a frozen overlay row has an unsettled clock, so the idle settle runs", () => {
    firstTurnWithLostRetires();
    send(FIRST_TURN_START + 62_000, { role: "llm_done", agent_id: AGENT });
    expect(store().turnActive).toBe(false);
    expect(liveIds()).toHaveLength(2);
    expect(hasUnsettledClock(store().items)).toBe(true);
  });

  it("a GET snapshot taken while no turn runs drops overlay rows the page still holds", () => {
    firstTurnWithLostRetires();
    act(() => {
      store().processConnectionEvent({ type: "closed" });
      store().processConnectionEvent({ type: "open" });
    });
    act(() => {
      store().reloadSnapshot([reasoning("h1735249.0", "2026-11-26T03:47:15.865Z", 26642)], 10, false);
    });
    expect(liveIds()).toEqual([]);
  });

  it("a GET snapshot taken mid-turn keeps the overlay row that is still being written", () => {
    send(
      FIRST_TURN_START,
      { role: "reasoning_start", agent_id: AGENT, item_id: "live.t1.msg_a:0" },
      { role: "reasoning_delta", agent_id: AGENT, item_id: "live.t1.msg_a:0", content: "still going" },
    );
    expect(store().turnActive).toBe(true);
    act(() => {
      store().reloadSnapshot([reasoning("h1.0", "2026-11-26T03:40:00.000Z", 10)], 10, false);
    });
    expect(liveIds()).toEqual(["live.t1.msg_a:0"]);
  });
});

describe("a host that was replaced mid-turn leaves its overlay rows behind", () => {
  it("the new host's turn shows its own overlay rows; the old rows go when the agent goes idle", () => {
    send(
      FIRST_TURN_START,
      { role: "reasoning_start", agent_id: AGENT, item_id: "live.old.msg_a:0" },
      { role: "reasoning_delta", agent_id: AGENT, item_id: "live.old.msg_a:0", content: "old host" },
    );
    send(
      SECOND_TURN_START,
      { role: "chat_start", agent_id: AGENT, item_id: "live.new.msg_b:1" },
      { role: "chat_delta", agent_id: AGENT, item_id: "live.new.msg_b:1", content: "new host" },
    );
    expect(liveIds()).toEqual(["live.old.msg_a:0", "live.new.msg_b:1"]);

    act(() => store().settleTurn(SECOND_TURN_START + 5_000));
    expect(liveIds()).toEqual([]);
  });
});

describe("the Working clock of a later turn", () => {
  it("does not start from an overlay row left by an earlier turn, over a multi-step turn", () => {
    firstTurnWithLostRetires();
    secondTurn();
    const steps = [
      ["h2035137.0", "2026-11-26T04:08:21.997Z", 2339],
      ["h2059124.0", "2026-11-26T04:08:33.366Z", 3442],
    ] as const;
    for (const [id, at, ms] of steps) {
      send(Date.parse(at), snapshot([reasoning(id, at, ms), bash(`${id}.b`.replace(".0.b", ".1"), at)]));
    }
    send(
      Date.parse("2026-11-26T04:08:20.000Z"),
      { role: "reasoning_start", agent_id: AGENT, item_id: "live.t2.msg_z:0" },
    );
    const working = liveWorkClockMs(SCREENSHOT_AT);
    expect(working).toBeLessThan(40_000);
  });

  it("is not pulled earlier by the overlay row of its own turn", () => {
    secondTurn();
    send(
      Date.parse("2026-11-26T04:07:00.000Z"),
      { role: "reasoning_start", agent_id: AGENT, item_id: "live.t2.msg_z:0" },
    );
    const items = store().items;
    const run = items.filter((it) => it.kind !== "inbound_chat");
    expect(summarizeTurn(run).liveSegmentStartMs).toBe(Date.parse("2026-11-26T04:08:10.993Z") - 2455);
  });
});
