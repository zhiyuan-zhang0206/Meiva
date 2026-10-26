import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  TimelineResponse,
} from "../../contracts/types";
import { useTimelineStore } from "../timeline-store";
import { useTimeline } from "../use-timeline";
import type { UseTimelineTestContext } from "./use-timeline-context";

export function registerUseTimelineStreamingTests(ctx: UseTimelineTestContext): void {
  const {
    timeline,
    timelineReads,
    turnStarts,
    resolves,
    startTransitionSpy,
    pushEvent,
    pushBatch,
    snapshotItem,
    tlResp,
    wallClock,
    wrapper,
  } = ctx;

describe("useTimeline streamingCode flag", () => {
  it("code_start sets streamingCode=true; exec_start clears it back to false", async () => {
    const showError = vi.fn();

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));

    expect(result.current.streamingCode).toBe(false);
    pushEvent({ role: "code_start", item_id: "test", agent_id: 42 });
    expect(result.current.streamingCode).toBe(true);
    pushEvent({ role: "exec_start", item_id: "5.0", agent_id: 42 });
    expect(result.current.streamingCode).toBe(false);
  });
});

describe("useTimeline wall clock", () => {
  // meiva:note
  // The hook stamps events and settles turns with the wall clock the page provides, not the
  // process clock: every instant below is one the test chose.
  it("stamps a streamed event with the provided instant and freezes its clock at the next one", async () => {
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));
    const started = wallClock.now();

    pushEvent({ role: "code_start", item_id: "5.1", agent_id: 42 });
    expect(result.current.items[0].codeStartedAt).toBe(started);

    wallClock.advance(4200);
    pushEvent({ role: "exec_start", item_id: "5.2", agent_id: 42 });
    expect(result.current.items[0].codeElapsedMs).toBe(4200);
  });

  it("stamps every event of a frame batch with the provided instant", async () => {
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));
    const started = wallClock.now();

    pushBatch([{ role: "code_start", item_id: "7.0", agent_id: 42 }]);

    expect(result.current.items[0].codeStartedAt).toBe(started);
  });
});

describe("useTimeline token_usage (single subscription — R10)", () => {
  it("token_usage is NOT folded by useTimeline — the token state is owned solely by useTokenUsage", async () => {
    const showError = vi.fn();

    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));

    expect(useTimelineStore.getState().activeThreadId).toBe(42);
    useTimelineStore.setState({ tokenUsage: 0, reasoningTokens: 0 });

    pushEvent({
      role: "token_usage",
      agent_id: 42,
      input_tokens: 1234,
      output_tokens: 0,
      reasoning_tokens: 56,
    });

    expect(useTimelineStore.getState().tokenUsage).toBe(0);
    expect(useTimelineStore.getState().reasoningTokens).toBe(0);
  });
});

describe("useTimeline full two-turn conversation overall order (e2e)", () => {
  it("both turns pushed via timeline_snapshot; order strictly aligned by item_id", async () => {
    const showError = vi.fn();
    timeline.queue(resolves(tlResp([])));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toHaveLength(0);

    pushEvent({ role: "inbound_arrived", agent_id: 42, inbound_id: 100, kind: "chat", source: "user", content: "turn 1" });

    pushEvent({ role: "reasoning_start", item_id: "3.0", agent_id: 42 });
    pushEvent({ role: "reasoning_delta", item_id: "3.0", agent_id: 42, content: "think1" });
    pushEvent({ role: "chat_start", item_id: "3.1", agent_id: 42 });
    pushEvent({ role: "chat_delta", item_id: "3.1", agent_id: 42, content: "reply1" });
    pushEvent({ role: "code_start", item_id: "3.2", agent_id: 42 });
    pushEvent({ role: "code_delta", item_id: "3.2", agent_id: 42, content: "code1" });
    pushEvent({ role: "exec_start", item_id: "4.0", agent_id: 42 });
    pushEvent({ role: "exec_output", item_id: "4.0", agent_id: 42, content: "output1" });

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "2.0", kind: "inbound_chat", payload: "turn 1 env", inbound_id: 100 }),
        snapshotItem({ item_id: "3.0", kind: "agent_reasoning", payload: "think1" }),
        snapshotItem({ item_id: "3.1", kind: "agent_chat", payload: "reply1" }),
        snapshotItem({ item_id: "3.2", kind: "agent_code", payload: "code1" }),
        snapshotItem({ item_id: "4.0", kind: "code_output", payload: "output1" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items).toHaveLength(5);
      const kinds = result.current.items.map((i) => i.kind);
      expect(kinds).toEqual(["inbound_chat", "agent_reasoning", "agent_chat", "agent_code", "code_output"]);
      const ids = result.current.items.map((i) => i.item_id);
      expect(ids).toEqual(["2.0", "3.0", "3.1", "3.2", "4.0"]);
    });

    pushEvent({ role: "inbound_arrived", agent_id: 42, inbound_id: 101, kind: "chat", source: "user", content: "turn 2" });
    pushEvent({ role: "reasoning_start", item_id: "6.0", agent_id: 42 });
    pushEvent({ role: "reasoning_delta", item_id: "6.0", agent_id: 42, content: "think2" });
    pushEvent({ role: "chat_start", item_id: "6.1", agent_id: 42 });
    pushEvent({ role: "chat_delta", item_id: "6.1", agent_id: 42, content: "reply2" });

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "2.0", kind: "inbound_chat", payload: "turn 1 env", inbound_id: 100 }),
        snapshotItem({ item_id: "3.0", kind: "agent_reasoning", payload: "think1" }),
        snapshotItem({ item_id: "3.1", kind: "agent_chat", payload: "reply1" }),
        snapshotItem({ item_id: "3.2", kind: "agent_code", payload: "code1" }),
        snapshotItem({ item_id: "4.0", kind: "code_output", payload: "output1" }),
        snapshotItem({ item_id: "5.0", kind: "inbound_chat", payload: "turn 2 env", inbound_id: 101 }),
        snapshotItem({ item_id: "6.0", kind: "agent_reasoning", payload: "think2" }),
        snapshotItem({ item_id: "6.1", kind: "agent_chat", payload: "reply2" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items).toHaveLength(8);
      const kinds = result.current.items.map((i) => i.kind);
      expect(kinds).toEqual([
        "inbound_chat", "agent_reasoning", "agent_chat", "agent_code", "code_output",
        "inbound_chat", "agent_reasoning", "agent_chat",
      ]);
    });
  });

  it("when snapshot contains the commit version, replaces partial (item_id matches)", async () => {

    const showError = vi.fn();

    timeline.queue(resolves(tlResp([
      snapshotItem({ item_id: "2.0", kind: "inbound_chat", payload: "turn 1 env", inbound_id: 100 }),
    ])));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "2.0", kind: "inbound_chat", payload: "turn 1 env", inbound_id: 100 }),
        snapshotItem({ item_id: "3.0", kind: "agent_chat", payload: "reply commit" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items).toHaveLength(2);
      expect(result.current.items[1].item_id).toBe("3.0");
      expect(result.current.items[1].partial).toBeUndefined();
    });
  });
});

describe("useTimeline startTransition split (isStreamingDelta cluster)", () => {

  beforeEach(() => {
    startTransitionSpy.mockClear();
  });

  it.each([
    ["code_delta", { role: "code_delta", item_id: "1", agent_id: 42, content: "x" }],
    ["chat_delta", { role: "chat_delta", item_id: "1", agent_id: 42, content: "y" }],
    ["reasoning_delta", { role: "reasoning_delta", item_id: "1", agent_id: 42, content: "z" }],
    ["exec_output_chunk", { role: "exec_output_chunk", item_id: "1", agent_id: 42, content: "w" }],
  ] as const)(
    "%s wraps in startTransition (kills L167-172 streaming role / equality / if-branch mutations)",
    async (_label, ev) => {
      const showError = vi.fn();
      renderHook(() => useTimeline(42, showError), { wrapper });
      await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));

      startTransitionSpy.mockClear();
      pushEvent(ev);

      expect(startTransitionSpy).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    ["reasoning_start", { role: "reasoning_start", item_id: "1", agent_id: 42 }],
    ["chat_start", { role: "chat_start", item_id: "1", agent_id: 42 }],
    ["code_start", { role: "code_start", item_id: "1", agent_id: 42 }],
    ["exec_start", { role: "exec_start", item_id: "5.0", agent_id: 42 }],
    ["llm_done", { role: "llm_done", agent_id: 42 }],
    [
      "inbound_arrived",
      {
        role: "inbound_arrived",
        agent_id: 42,
        inbound_id: 1,
        kind: "chat",
        source: "user",
        content: "hi",
      },
    ],
  ] as const)(
    "%s does not go through startTransition (kills L167-172 if-branch false path + role equality reverse mutation)",
    async (_label, ev) => {
      const showError = vi.fn();
      renderHook(() => useTimeline(42, showError), { wrapper });
      await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));

      startTransitionSpy.mockClear();
      pushEvent(ev);

      expect(startTransitionSpy).toHaveBeenCalledTimes(0);
    },
  );

  it("if(isStreamingDelta) → true mutation: non-streaming events also wrongly wrapped (kills L172 true)", async () => {

    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));

    startTransitionSpy.mockClear();
    pushEvent({ role: "exec_start", item_id: "5.0", agent_id: 42 });

    expect(startTransitionSpy).not.toHaveBeenCalled();
  });

  it("if(isStreamingDelta) → false mutation: streaming events miss transition (kills L172 false)", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));

    startTransitionSpy.mockClear();
    pushEvent({ role: "code_delta", item_id: "1", agent_id: 42, content: "x" });

    expect(startTransitionSpy).toHaveBeenCalledTimes(1);
  });
});

describe("useTimeline data effect agentId==null guard (L135)", () => {
  it("agentId=null with pre-filled query cache: data effect does not call reloadSnapshot (kills L135 agentId==null → false)", async () => {

    const showError = vi.fn();

    ctx.getQueryClient().setQueryData<TimelineResponse>(
      ["timeline", null],
      tlResp([snapshotItem({ payload: "prefilled null cache" })]),
    );

    const { result } = renderHook(() => useTimeline(null, showError), { wrapper });

    await ctx.delay(0);

    expect(result.current.items).toEqual([]);
    expect(useTimelineStore.getState().items).toEqual([]);
  });
});

describe("useTimeline SSE batch path", () => {
  it("a frame batch folds into the expected final timeline state", async () => {
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));
    await waitFor(() => expect(ctx.getQueryClient().isFetching()).toBe(0));

    await act(async () => {
      await Promise.resolve();
    });

    pushBatch([
      { role: "code_start", agent_id: 42, item_id: "7.0" },
      { role: "code_delta", agent_id: 42, item_id: "7.0", content: "pri" },
      { role: "code_delta", agent_id: 42, item_id: "7.0", content: "nt" },
      { role: "token_usage", agent_id: 42, input_tokens: 500, output_tokens: 10 },
    ]);

    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0].item_id).toBe("7.0");
    expect(result.current.items[0].payload).toBe("print");
    expect(result.current.streamingCode).toBe(true);
    expect(turnStarts).toContain(42);
  });
});

describe("impersonation timeline refresh", () => {
  it("loads committed external replies through the normal timeline query", async () => {
    const anchor = snapshotItem({ item_id: "4.0", kind: "inbound_chat", payload: "Session started" });
    const reply = snapshotItem({ item_id: "4.2", payload: "Fix verified", source: "agent:42",
      impersonation: { agent_id: 42, session_id: 0, name: "Fix login", executor_name: "Codex: helper",
        provider: "codex", anchor_item_id: "4.0", seq: 1 } });
    timeline.queue(resolves(tlResp([anchor])));
    timeline.always(resolves(tlResp([anchor, reply])));
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    pushEvent({ role: "impersonation_changed", agent_id: 42 });
    await waitFor(() => expect(result.current.items.map((entry) => entry.payload)).toContain("Fix verified"));
    pushEvent({ role: "impersonation_changed", agent_id: 42 });
    await waitFor(() => expect(timelineReads).toHaveLength(3));
    expect(result.current.items.filter((entry) => entry.item_id === "4.2")).toHaveLength(1);
    expect(result.current.items.find((entry) => entry.item_id === "4.2")?.impersonation?.executor_name).toBe("Codex: helper");
  });
});

}
