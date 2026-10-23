import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  TimelineResponse,
} from "../../contracts/types";
import { useTimeline } from "../use-timeline";
import type { UseTimelineTestContext } from "./use-timeline-context";

export function registerUseTimelineSseTests(ctx: UseTimelineTestContext): void {
  const {
    api,
    pushEvent,
    pushConnectionEvent,
    snapshotItem,
    tlResp,
    composedSnapshot,
    wrapper,
  } = ctx;

describe("useTimeline SSE LLM streaming flow", () => {
  it("partial chat → timeline_snapshot replaces the partial", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ item_id: "1.0", kind: "inbound_chat", payload: "user msg", inbound_id: 1 }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({
      role: "chat_delta",
      item_id: "2.0",
      agent_id: 42,
      content: "...continued reply",
    });
    expect(result.current.items[1]).toMatchObject({
      kind: "agent_chat",
      payload: "...continued reply",
      partial: true,
    });

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "1.0", kind: "inbound_chat", payload: "user msg", inbound_id: 1 }),
        snapshotItem({ item_id: "2.0", kind: "agent_chat", payload: "Here's my full reply ...continued reply" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items[1].partial).toBeUndefined();
      expect(result.current.items[1].payload).toContain("Here's my full reply");
    });
  });

  it("streaming reasoning → timeline_snapshot replaces", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(0));

    pushEvent({ role: "reasoning_start", item_id: "2.0", agent_id: 42 });
    pushEvent({ role: "reasoning_delta", item_id: "2.0", agent_id: 42, content: "thinking..." });
    expect(result.current.items[0]).toMatchObject({
      kind: "agent_reasoning",
      payload: "thinking...",
    });

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "2.0", kind: "agent_reasoning", payload: "thinking... done" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items[0].payload).toBe("thinking... done");
    });
  });

  it("user sends a message mid-stream → timeline_snapshot fixes up", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ kind: "code_output", payload: "old" }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({ role: "reasoning_start", item_id: "test", agent_id: 42 });
    pushEvent({ role: "reasoning_delta", item_id: "test", agent_id: 42, content: "Let me think " });
    pushEvent({ role: "reasoning_delta", item_id: "test", agent_id: 42, content: "about it..." });
    expect(result.current.items[1]).toMatchObject({
      kind: "agent_reasoning",
      payload: "Let me think about it...",
    });

    pushEvent({
      role: "inbound_arrived",
      agent_id: 42,
      inbound_id: 200,
      kind: "chat",
      source: "user",
      content: "wait, also...",
    });

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ kind: "code_output", payload: "old" }),
        snapshotItem({ kind: "agent_reasoning", payload: "Let me think about it... done" }),
        snapshotItem({ kind: "agent_chat", payload: "OK", inbound_id: null }),
      ],
    });

    await waitFor(() => {
      const reasoning = result.current.items.find((i) => i.kind === "agent_reasoning");
      expect(reasoning?.payload).toBe("Let me think about it... done");
      expect(reasoning?.partial).toBeUndefined();
    });
  });
});

describe("useTimeline error & cancellation paths", () => {
  it("getTimeline initial load failure clears partials", async () => {
    const showError = vi.fn();

    vi.mocked(api.getTimeline).mockRejectedValueOnce(new Error("network down"));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });

    pushEvent({ role: "chat_delta", item_id: "test", agent_id: 42, content: "in progress" });
    expect(result.current.items[0]).toMatchObject({ partial: true });

    await waitFor(() => expect(showError).toHaveBeenCalled());
    await waitFor(() => {
      expect(result.current.items[0]?.partial).toBeFalsy();
    });
    expect(result.current.items[0]?.payload).toBe("in progress");
  });

  it("cancelled flag race: after agent switch, old fetch resolve does not contaminate new agent items", async () => {
    const showError = vi.fn();

    let resolveStaleA: (v: TimelineResponse) => void = () => undefined;
    vi.mocked(api.getTimeline).mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolveStaleA = r;
        }),
    );
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ payload: "thread B fresh" }),
    ]));

    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );

    rerender({ tid: 2 });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("thread B fresh"));

    resolveStaleA(tlResp([snapshotItem({ payload: "STALE THREAD A" })]));
    await ctx.delay(10);

    expect(result.current.items.find((i) => i.payload === "STALE THREAD A")).toBeUndefined();
    expect(result.current.items[0].payload).toBe("thread B fresh");
    unmount();
  });
});

describe("useTimeline connectionState", () => {
  it("default connectionState='open'", async () => {
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());
    expect(result.current.connectionState).toBe("open");
  });

  it("reconnecting raises no toast; restores after open", async () => {
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    pushConnectionEvent({ type: "reconnecting" });
    expect(result.current.connectionState).toBe("reconnecting");
    expect(showError).not.toHaveBeenCalled();

    pushConnectionEvent({ type: "open" });
    expect(result.current.connectionState).toBe("open");
  });

  it("closed only changes state, no toast (single-channel banner, avoids double-noise)", async () => {
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    pushConnectionEvent({ type: "closed" });
    expect(result.current.connectionState).toBe("closed");
    expect(showError).not.toHaveBeenCalled();
  });

  it("on closed, partial flags cleared + streamingCode=false (SSE dead, stream won't resume)", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([]));
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toEqual([]));

    pushEvent({ role: "code_start", item_id: "5.1", agent_id: 42 });
    pushEvent({ role: "code_delta", item_id: "5.1", agent_id: 42, content: "x = 1" });
    expect(result.current.streamingCode).toBe(true);

    pushEvent({
      role: "chat_delta",
      item_id: "5.0",
      agent_id: 42,
      content: "...",
    });
    expect(result.current.items.find((i) => i.partial)).toBeDefined();

    pushConnectionEvent({ type: "closed" });

    expect(result.current.items.find((i) => i.partial)).toBeDefined();
    expect(result.current.items.find((i) => i.interrupted)).toBeDefined();
    expect(result.current.streamingCode).toBe(false);

    pushConnectionEvent({ type: "open" });
    expect(result.current.items.find((i) => i.interrupted)).toBeUndefined();

    expect(result.current.items.find((i) => i.partial)).toBeDefined();
  });

  it("on agentId switch, connectionState is PRESERVED (Task #1051: the all-events stream is one shared connection — switchThread no longer stamps 'open', which would clear the disconnect banner mid-reconnect)", async () => {
    const showError = vi.fn();
    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    pushConnectionEvent({ type: "closed" });
    expect(result.current.connectionState).toBe("closed");

    rerender({ tid: 2 });

    expect(result.current.connectionState).toBe("closed");

    pushConnectionEvent({ type: "open" });
    rerender({ tid: 3 });
    expect(result.current.connectionState).toBe("open");
    unmount();
  });

  it("opening the selected stream reconciles through one composed snapshot read", async () => {
    vi.mocked(api.getTimeline).mockResolvedValue(
      tlResp([snapshotItem({ item_id: "1.0", payload: "initial read" })]),
    );
    vi.mocked(api.getConversationSnapshot).mockReset().mockResolvedValue(
      composedSnapshot(tlResp([
        snapshotItem({ item_id: "1.0", payload: "reconciled after gap" }),
        snapshotItem({ item_id: "2.0", payload: "committed while away" }),
      ])),
    );
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("initial read"));

    pushConnectionEvent({ type: "open" });

    await waitFor(() =>
      expect(result.current.items.map((item) => item.payload)).toEqual(
        ["reconciled after gap", "committed while away"],
      ),
    );
    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);

    expect(api.getTimeline).toHaveBeenCalledTimes(1);
  });

  it("open with no active agent does not reconcile a snapshot", async () => {
    renderHook(() => useTimeline(null, vi.fn()), { wrapper });

    pushConnectionEvent({ type: "open" });
    await act(async () => { await ctx.delay(20); });

    expect(api.getConversationSnapshot).not.toHaveBeenCalled();
  });

  it("parse-failed: first toast fires; subsequent same-error toasts deduped (no flood)", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    const sameError = new SyntaxError("Unexpected token");
    pushConnectionEvent({ type: "parse-failed", raw: "x", error: sameError });
    pushConnectionEvent({ type: "parse-failed", raw: "y", error: sameError });
    pushConnectionEvent({ type: "parse-failed", raw: "z", error: sameError });

    const parseFails = showError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    expect(parseFails).toHaveLength(1);
  });

  it("parse-failed different errors → each reported once", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    pushConnectionEvent({
      type: "parse-failed",
      raw: "x",
      error: new SyntaxError("err A"),
    });
    pushConnectionEvent({
      type: "parse-failed",
      raw: "y",
      error: new SyntaxError("err B"),
    });

    const parseFails = showError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    expect(parseFails).toHaveLength(2);
  });

  it("open event resets the parse-failed dedupe set (new connection re-enables toast)", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    const err = new SyntaxError("dup");
    pushConnectionEvent({ type: "parse-failed", raw: "x", error: err });
    pushConnectionEvent({ type: "open" });
    pushConnectionEvent({ type: "parse-failed", raw: "y", error: err });

    const parseFails = showError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    expect(parseFails).toHaveLength(2);
  });

  it("showError prop switch → new showError receives parse-failed toast (kills L209 dep [])", async () => {

    const firstShowError = vi.fn();
    const secondShowError = vi.fn();
    const { rerender } = renderHook(
      ({ se }: { se: (msg: string) => void }) => useTimeline(42, se),
      { initialProps: { se: firstShowError }, wrapper },
    );
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    rerender({ se: secondShowError });

    pushConnectionEvent({
      type: "parse-failed",
      raw: "x",
      error: new SyntaxError("after rebind"),
    });

    const newCalls = secondShowError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    const oldCalls = firstShowError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    expect(newCalls).toHaveLength(1);
    expect(oldCalls).toHaveLength(0);
  });
});

}
