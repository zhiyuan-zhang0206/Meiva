import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  inspectLiveQueryKey,
  type SystemEvent,
  type TimelineResponse,
} from "../../contracts/types";
import { useTimelineStore } from "../timeline-store";
import { useTimeline } from "../use-timeline";
import type { UseTimelineTestContext } from "./use-timeline-context";

export function registerUseTimelineCompactTests(ctx: UseTimelineTestContext): void {
  const {
    timeline,
    wireReplies,
    timelineReads,
    turnStarts,
    resolves,
    seedSettings,
    pushEvent,
    snapshotItem,
    postWindow,
    tlResp,
    wrapper,
  } = ctx;

describe("useTimeline compact history sessions", () => {
  it("walks newest-first across two previous segments when display.compact_history_sessions = 2", async () => {

    const showError = vi.fn();
    seedSettings({ "display.compact_history_sessions": 2 });
    const pageFor = (opts?: { before?: string; limit?: number }): TimelineResponse => {
      if (opts?.before === "2.0") {
        return tlResp(
          [
            snapshotItem({
              item_id: "s1.seg.0.0",
              kind: "inbound_compact_summary",
              payload: "previous summary",
            }),
            snapshotItem({ item_id: "s1.seg.4.0", kind: "agent_chat", payload: "previous session" }),
          ],
          true,
        );
      }
      if (opts?.before === "s1.seg.0.0") {
        return tlResp(
          [snapshotItem({ item_id: "s2.seg.4.0", kind: "agent_chat", payload: "older session" })],
          false,
        );
      }
      if (opts?.before !== undefined) return tlResp([], false);
      return tlResp(postWindow(), true);
    };
    timeline.always(resolves(pageFor()));
    for (const before of ["2.0", "s1.seg.0.0"]) {
      wireReplies.set(`/api/agents/42/timeline?before=${before}`, [resolves(pageFor({ before }))]);
    }

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    expect(result.current.hasMoreOlder).toBe(true);

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 3,
      items: postWindow(),
    });

    await waitFor(() => {
      expect(timelineReads).toContainEqual({ agentId: 42, before: "s1.seg.0.0", limit: 100, signal: expect.any(AbortSignal) as AbortSignal });
    });
    await waitFor(() => {
      expect(result.current.items.map((i) => i.item_id)).toEqual([
        "s2.seg.4.0",
        "s1.seg.0.0",
        "s1.seg.4.0",
        "0.0",
        "1.0",
        "2.0",
      ]);
    });

    expect(timelineReads.map((read) => read.before).filter((before) => before !== undefined)).toEqual(["2.0", "s1.seg.0.0"]);
    expect(showError).not.toHaveBeenCalled();
  });

  it("All walks through a long segment and more than three previous pages, then stops at has_more=false", async () => {
    seedSettings({ "display.compact_history_sessions": -1 });
    const pages: Record<string, TimelineResponse> = {
      "2.0": tlResp([snapshotItem({ item_id: "1.5", payload: "same long segment" })], true),
      "1.5": tlResp([snapshotItem({ item_id: "s1.a.0.0", kind: "inbound_compact_summary", payload: "first" })], true),
      "s1.a.0.0": tlResp([snapshotItem({ item_id: "s2.b.0.0", kind: "inbound_compact_summary", payload: "second" })], true),
      "s2.b.0.0": tlResp([snapshotItem({ item_id: "s3.c.0.0", kind: "inbound_compact_summary", payload: "third" })], true),
      "s3.c.0.0": tlResp([snapshotItem({ item_id: "s4.d.0.0", kind: "inbound_compact_summary", payload: "fourth" })], false),
    };
    timeline.always(resolves(tlResp(postWindow(), true)));
    for (const [before, page] of Object.entries(pages)) {
      wireReplies.set(`/api/agents/42/timeline?before=${before}`, [resolves(page)]);
    }
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(result.current.items.some((item) => item.item_id === "s4.d.0.0")).toBe(true));
    const beforeCalls = () => timelineReads.map((read) => read.before).filter((before) => before !== undefined);
    expect(beforeCalls()).toEqual(["2.0", "1.5", "s1.a.0.0", "s2.b.0.0", "s3.c.0.0"]);
    act(() => useTimelineStore.setState((state) => ({ items: [...state.items, snapshotItem({ item_id: "3.0" })] })));
    await ctx.delay(20);
    expect(beforeCalls()).toHaveLength(5);
  });

  it("All waits for the post-compact tail read before deciding whether older history exists", async () => {
    seedSettings({ "display.compact_history_sessions": -1 });
    let postTailResolve: ((value: TimelineResponse) => void) | null = null;
    timeline.always(resolves(tlResp([
      snapshotItem({ item_id: "90.0", payload: "before compact" }),
    ], false)));
    wireReplies.set("/api/agents/42/timeline?before=2.0", [resolves(tlResp([
      snapshotItem({ item_id: "s1.a.1.0", payload: "retained" }),
    ], false))]);
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("before compact"));
    timeline.always(new Promise<TimelineResponse>((resolve) => { postTailResolve = resolve; }).then(resolves));
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(timelineReads.filter((read) => read.before === undefined)).toHaveLength(2));
    expect(timelineReads.filter((read) => read.before)).toHaveLength(0);
    act(() => { postTailResolve?.(tlResp(postWindow(), true)); });
    await waitFor(() => expect(result.current.items.some((item) => item.payload === "retained")).toBe(true));
    expect(timelineReads.filter((read) => read.before)).toHaveLength(1);
  });

  it("All retries a failed history page and stops after the successful terminal page", async () => {
    seedSettings({ "display.compact_history_sessions": -1 });
    timeline.always(resolves(tlResp(postWindow(), true)));
    wireReplies.set("/api/agents/42/timeline?before=2.0", [
      { kind: "json", status: 503, body: { detail: "transient" } },
      resolves(tlResp([snapshotItem({ item_id: "s1.a.1.0", payload: "recovered" })], false)),
    ]);
    const olderAttempts = () => timelineReads.filter((read) => read.before !== undefined).length;
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(olderAttempts()).toBe(1));
    act(() => useTimelineStore.setState((state) => ({ items: [...state.items, snapshotItem({ item_id: "3.0" })] })));
    await waitFor(() => expect(result.current.items.some((item) => item.payload === "recovered")).toBe(true));
    expect(olderAttempts()).toBe(2);
    expect(showError).toHaveBeenCalledWith("Failed to load older messages: transient");
  });

  it("display.compact_history_sessions = 0 keeps the legacy clear-on-compact behavior (no retention fetch)", async () => {
    const showError = vi.fn();
    seedSettings({ "display.compact_history_sessions": 0 });
    const postItems = [
      snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
      snapshotItem({ item_id: "1.0", kind: "agent_chat", payload: "post-compact" }),
    ];
    timeline.always(resolves(tlResp(postItems, true)));
    timeline.queue(
      resolves(tlResp([snapshotItem({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })], true)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 2, items: postItems });
    await ctx.delay(20);

    const beforeCalls = timelineReads.filter((read) => read.before !== undefined);
    expect(beforeCalls).toHaveLength(0);
    expect(result.current.items.map((i) => i.item_id)).toEqual(["0.0", "1.0"]);
  });

  it("the post-compact snapshot also invalidates the agent's inspect / pending / token-usage caches (task #1959)", async () => {

    const showError = vi.fn();
    const queryClient = ctx.getQueryClient();
    const cachedElsewhere = [inspectLiveQueryKey(42), ["agent-inspect", 42], ["pending", 42], ["token-usage", 42]];
    for (const key of cachedElsewhere) queryClient.setQueryData(key, null);
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(timelineReads).toHaveLength(1));
    const invalidated = () => cachedElsewhere.map((key) => queryClient.getQueryState(key)?.isInvalidated);

    pushEvent({ role: "compact_done", agent_id: 42 });
    await ctx.delay(0);
    expect(invalidated()).toEqual([false, false, false, false]);
    expect(timelineReads).toHaveLength(1);

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 2,
      items: [snapshotItem({ item_id: "1.0", payload: "[summary]" })],
    });
    await waitFor(() => expect(timelineReads).toHaveLength(2));
    expect(invalidated()).toEqual([true, true, true, true]);
  });
});

describe("useTimeline SSE inbound flow", () => {
  it("notes every event role that starts the timeline turn state", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(timelineReads.length).toBeGreaterThan(0));

    const starts: SystemEvent[] = [
      { role: "inbound_arrived", agent_id: 42, inbound_id: 1, kind: "chat", source: "user", content: "hi" },
      { role: "chat_start", agent_id: 42, item_id: "1.0" },
      { role: "code_start", agent_id: 42, item_id: "1.1" },
      { role: "reasoning_start", agent_id: 42, item_id: "1.2" },
      { role: "exec_start", agent_id: 42, item_id: "1.3" },
    ];
    for (const event of starts) pushEvent(event);

    expect(turnStarts).toEqual(starts.map(() => 42));
  });

  it("inbound_arrived no longer produces an optimistic placeholder — turnActive flips true immediately; items unchanged", async () => {
    const showError = vi.fn();
    timeline.always(resolves(tlResp([
      snapshotItem({ kind: "code_output", payload: "previous" }),
    ])));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({
      role: "inbound_arrived",
      agent_id: 42,
      inbound_id: 100,
      kind: "chat",
      source: "user",
      content: "hello idle agent",
    });

    expect(result.current.items).toHaveLength(1);

    expect(result.current.turnActive).toBe(true);

    await waitFor(() => expect(timelineReads).toHaveLength(2));
  });

  it("timeline_snapshot push delivers envelope-wrap inbound_chat into the timeline", async () => {
    const showError = vi.fn();
    timeline.queue(resolves(tlResp([
      snapshotItem({ item_id: "1.0", kind: "code_output", payload: "previous" }),
    ])));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({
      role: "inbound_arrived",
      agent_id: 42,
      inbound_id: 100,
      kind: "chat",
      source: "user",
      content: "msg",
    });

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "1.0", kind: "code_output", payload: "previous" }),
        snapshotItem({
          item_id: "2.0",
          kind: "inbound_chat",
          payload: "User:\nmsg",
          inbound_id: 100,
        }),
      ],
    });

    await waitFor(() => {
      const inbound = result.current.items.find((i) => i.inbound_id === 100);
      expect(inbound?.payload).toContain("User:");
    });
    expect(result.current.items).toHaveLength(2);
  });
});

}
