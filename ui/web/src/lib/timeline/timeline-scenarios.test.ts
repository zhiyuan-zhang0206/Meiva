// meiva:note
// End-to-end timeline scenario tests — does not boot React DOM;
// simulates the events + reload chain that useTimeline runs through.
// Covers useTimeline business logic (applySystemEvent +
// mergeSnapshotWithStreaming) without pulling in jsdom / RTL deps.
//
// `Simulator` mirrors useTimeline hook behavior:
//   - feed(ev): applies an SSE event; reload-trigger events
//     (inbound_committed / llm_done) are no-ops (the test manually
//     controls reload timing to simulate server delay).
//   - reload(snapshot): simulates merging a GET /timeline snapshot into items.
//   - items: the list the UI currently sees.
//
// Difference from the real hook: the real hook uses React state +
// setReloadKey to trigger a useEffect that pulls the snapshot; here
// the test **manually** calls reload to control timing, which can
// simulate "snapshot early / late" races. Reducer behavior itself is
// identical to the real hook (uses the same applySystemEvent +
// mergeSnapshotWithStreaming).

import { describe, expect, it } from "vitest";

import { applySystemEvent, mergeSnapshotWithStreaming } from "./timeline";
import type { BackendTimelineItem, SystemEvent } from "../contracts/types";

class Simulator {
  items: BackendTimelineItem[] = [];

  feed(ev: SystemEvent): void {
    if (ev.role === "inbound_committed" || ev.role === "llm_done") {
      // meiva:note
      // reload trigger events — the test manually calls reload(snapshot) to simulate server delay
      return;
    }
    this.items = applySystemEvent(this.items, ev);
  }

  reload(snapshot: BackendTimelineItem[]): void {
    this.items = mergeSnapshotWithStreaming(this.items, snapshot, 0);
  }

  /** Called on agent switch / reset — the real hook calls setItems([]) when agentId changes. */
  resetThread(): void {
    this.items = [];
  }
}


let _id_counter_sim = 0;
function snapshotItem(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    item_id: `sim.${++_id_counter_sim}`,
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

describe("end-to-end timeline scenarios", () => {
  it("user sends a message while idle → wait for inbound_committed reload to get the server body into the list (no client echo)", () => {
    // meiva:note
    // History: earlier versions used client echo + dedupe to give the
    // user instant visual feedback for "the message I just sent", but
    // the echo (raw payload) → reload (server body) two-phase
    // render caused the blue bar to flicker — bad UX. Switched to
    // "render only the server-committed server body once", trading
    // ~100ms network + commit delay for visual cleanliness. The
    // slow-idle-wake race is no longer a problem after the SSE
    // inbound_committed refactor (event fires after commit).
    const sim = new Simulator();

    // meiva:note
    // initial state: agent has run before, currently idle, items end with code_output.
    // item_id is set explicitly so both reloads see the same id (in
    // production the gateway assigns the same id to the same
    // state.messages position).
    sim.reload([
      snapshotItem({ item_id: "1.0", kind: "code_output", payload: "previous" }),
    ]);
    expect(sim.items).toHaveLength(1);

    // meiva:note
    // User sends a message → POST /messages 200 → gateway publishes inbound_arrived
    sim.feed({
      role: "inbound_arrived",
      agent_id: 1,
      inbound_id: 100,
      kind: "chat",
      source: "user",
      content: "hello idle agent",
    });

    // meiva:note
    // Optimistic update removed — inbound_arrived produces no placeholder; the message renders via server body after reload.
    expect(sim.items).toHaveLength(1);

    // meiva:note
    // After the claim node processes it, publish inbound_committed → triggers reload
    sim.feed({ role: "inbound_committed", agent_id: 1, inbound_id: 100 });

    // meiva:note
    // After server commit, snapshot contains the server body version — old code_output keeps the same id
    sim.reload([
      snapshotItem({ item_id: "1.0", kind: "code_output", payload: "previous" }),
      snapshotItem({
        item_id: "2.0",
        kind: "inbound_chat",
        payload: "hello idle agent",
        inbound_id: 100,
      }),
    ]);

    expect(sim.items).toHaveLength(2);
    expect(sim.items[1].payload).toBe("hello idle agent");
  });

  it("user sends a message mid-reasoning → llm_done reload snapshot carries the commit reasoning", () => {
    // meiva:note
    // After the ChatAnthropic switch, thinking blocks live in
    // AIMessage.content; the gateway timeline endpoint splits thinking +
    // text into snapshot — at llm_done the streaming reasoning trail
    // is replaced by the commit version. The three kinds (reasoning /
    // chat / code) are symmetric.
    const sim = new Simulator();

    // meiva:note
    // initial idle reload
    sim.reload([snapshotItem({ kind: "code_output", payload: "old" })]);

    // meiva:note
    // agent starts a new turn — reasoning is streaming
    sim.feed({ role: "reasoning_start", item_id: "test", agent_id: 1 });
    sim.feed({ role: "reasoning_delta", item_id: "test", agent_id: 1, content: "Let me think " });
    sim.feed({ role: "reasoning_delta", item_id: "test", agent_id: 1, content: "about it..." });
    expect(sim.items).toHaveLength(2);
    expect(sim.items[1]).toMatchObject({
      kind: "agent_reasoning",
      payload: "Let me think about it...",
    });

    // meiva:note
    // user sends a message mid-reasoning: inbound_arrived no-op (optimistic update removed)
    sim.feed({
      role: "inbound_arrived",
      agent_id: 1,
      inbound_id: 200,
      kind: "chat",
      source: "user",
      content: "wait, also...",
    });
    // meiva:note
    // Optimistic update removed — inbound_arrived inserts no item
    expect(sim.items).toHaveLength(2);

    // meiva:note
    // Assume the current turn completes → llm_done. The server
    // commits an AIMessage containing thinking + text blocks; the
    // gateway splits agent_reasoning + agent_chat into the snapshot.
    sim.feed({ role: "llm_done", agent_id: 1 });
    sim.reload([
      snapshotItem({ kind: "code_output", payload: "old" }),
      snapshotItem({ kind: "agent_reasoning", payload: "Let me think about it... done" }),
      snapshotItem({ kind: "agent_chat", payload: "OK", inbound_id: null }),
    ]);

    // meiva:note
    // streaming reasoning trail replaced by snapshot full version (no partial flag)
    const reasoning = sim.items.find((i) => i.kind === "agent_reasoning");
    expect(reasoning).toMatchObject({ payload: "Let me think about it... done" });
    expect(reasoning?.partial).toBeUndefined();
    // meiva:note
    // Optimistic update removed — inbound_committed has not arrived
    // yet; the snapshot does not contain the server body with
    // inbound_id=200, and the timeline does not show the user's just-
    // sent message. The user must wait for inbound_committed → next
    // reload to see their message land in the timeline.
  });

  it("user enters mid-stream → partial mark + llm_done reload overrides", () => {
    // meiva:note
    // User refreshes the page → misses chat_start → receives
    // chat_delta directly. Streaming chat_delta and the commit-version
    // agent_chat (after reload) share the same item_id; the snapshot
    // automatically replaces the partial.
    const sim = new Simulator();
    sim.reload([
      snapshotItem({
        item_id: "1.0",
        kind: "inbound_chat",
        payload: "user msg",
        inbound_id: 1,
      }),
    ]);

    // meiva:note
    // Missed start; first event is delta
    sim.feed({
      role: "chat_delta",
      item_id: "2.0",
      agent_id: 1,
      content: "...continued reply",
    });
    expect(sim.items).toHaveLength(2);
    expect(sim.items[1]).toMatchObject({
      kind: "agent_chat",
      payload: "...continued reply",
      partial: true,
    });

    // meiva:note
    // more delta
    sim.feed({
      role: "chat_delta",
      item_id: "2.0",
      agent_id: 1,
      content: " from agent",
    });
    expect(sim.items[1]).toMatchObject({
      payload: "...continued reply from agent",
      partial: true,
    });

    // meiva:note
    // LLM done
    sim.feed({ role: "llm_done", agent_id: 1 });

    // meiva:note
    // reload fetches the full commit version (snapshot agent_chat carries no partial flag, same id)
    sim.reload([
      snapshotItem({
        item_id: "1.0",
        kind: "inbound_chat",
        payload: "user msg",
        inbound_id: 1,
      }),
      snapshotItem({
        item_id: "2.0",
        kind: "agent_chat",
        payload: "Here's my full reply ...continued reply from agent",
      }),
    ]);

    // meiva:note
    // partial replaced by the full version
    expect(sim.items).toHaveLength(2);
    expect(sim.items[1].partial).toBeUndefined();
    expect(sim.items[1].payload).toContain("Here's my full reply");
  });

  it("agent switch clears streaming partials; no cross-thread bleed", () => {
    const sim = new Simulator();

    // meiva:note
    // thread A is streaming
    sim.feed({ role: "reasoning_delta", item_id: "test", agent_id: 1, content: "thread A reasoning" });
    expect(sim.items).toHaveLength(1);

    // meiva:note
    // user switches to thread B
    sim.resetThread();
    expect(sim.items).toHaveLength(0);

    sim.reload([snapshotItem({ kind: "agent_chat", payload: "thread B msg" })]);
    expect(sim.items[0].payload).toBe("thread B msg");
    // meiva:note
    // thread A's reasoning did not leak through
    expect(sim.items.find((i) => i.payload.includes("thread A"))).toBeUndefined();
  });

  it("two rapid back-to-back inbounds (claim processes in one shot): wait for reload to fetch both server bodies at once", () => {
    const sim = new Simulator();
    sim.reload([]);

    // meiva:note
    // user sends two in quick succession (claim is waiting while idle; both enter the queue together)
    sim.feed({
      role: "inbound_arrived",
      agent_id: 1,
      inbound_id: 1,
      kind: "chat",
      source: "user",
      content: "first",
    });
    sim.feed({
      role: "inbound_arrived",
      agent_id: 1,
      inbound_id: 2,
      kind: "chat",
      source: "user",
      content: "second",
    });
    // meiva:note
    // Optimistic update removed — inbound_arrived inserts no item
    expect(sim.items).toHaveLength(0);

    // meiva:note
    // claim processes the batch → two inbound_committed events
    sim.feed({ role: "inbound_committed", agent_id: 1, inbound_id: 1 });
    sim.feed({ role: "inbound_committed", agent_id: 1, inbound_id: 2 });

    // meiva:note
    // One reload contains both server bodies (claim handles the whole batch in one go)
    sim.reload([
      snapshotItem({ kind: "inbound_chat", payload: "envelope:first", inbound_id: 1 }),
      snapshotItem({ kind: "inbound_chat", payload: "envelope:second", inbound_id: 2 }),
    ]);

    expect(sim.items).toHaveLength(2);
    expect(sim.items[0].payload).toBe("envelope:first");
    expect(sim.items[1].payload).toBe("envelope:second");
  });
});
