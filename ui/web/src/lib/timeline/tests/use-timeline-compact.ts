import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SETTINGS_QUERY_KEY } from "../../state/use-user-settings";

import type {
  SystemEvent,
  TimelineResponse,
} from "../../contracts/types";
import { useTimelineStore } from "../timeline-store";
import { useTimeline } from "../use-timeline";
import type { UseTimelineTestContext } from "./use-timeline-context";

export function registerUseTimelineCompactTests(ctx: UseTimelineTestContext): void {
  const {
    api,
    noteTurnStart,
    pushEvent,
    snapshotItem,
    postWindow,
    tlResp,
    wrapper,
  } = ctx;

describe("useTimeline compact history sessions", () => {
  it("walks newest-first across two previous segments when display.compact_history_sessions = 2", async () => {

    const showError = vi.fn();
    vi.mocked(api.getSettings).mockResolvedValue({
      settings: [
        { key: "display.compact_history_sessions", value: 2, updated_at: "2026-09-17T00:00:00Z" },
      ],
    });
    const fetches: (string | undefined)[] = [];
    const pageFor = (opts?: { before?: string; limit?: number }): TimelineResponse => {
      fetches.push(opts?.before);
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
    vi.mocked(api.getTimeline).mockImplementation((_id: number, opts?: { before?: string; limit?: number }) =>
      Promise.resolve(pageFor(opts)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    expect(result.current.hasMoreOlder).toBe(true);
    await waitFor(() => expect(ctx.getQueryClient().getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 3,
      items: postWindow(),
    });

    await waitFor(() => {
      expect(api.getTimeline).toHaveBeenCalledWith(42, { before: "s1.seg.0.0", limit: 100, signal: expect.any(AbortSignal) as AbortSignal });
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

    expect(fetches.filter((b) => b !== undefined)).toEqual(["2.0", "s1.seg.0.0"]);
    expect(showError).not.toHaveBeenCalled();
  });

  it("All walks through a long segment and more than three previous pages, then stops at has_more=false", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ settings: [
      { key: "display.compact_history_sessions", value: -1, updated_at: "2026-09-24T00:00:00Z" },
    ] });
    const pages: Record<string, TimelineResponse> = {
      "2.0": tlResp([snapshotItem({ item_id: "1.5", payload: "same long segment" })], true),
      "1.5": tlResp([snapshotItem({ item_id: "s1.a.0.0", kind: "inbound_compact_summary", payload: "first" })], true),
      "s1.a.0.0": tlResp([snapshotItem({ item_id: "s2.b.0.0", kind: "inbound_compact_summary", payload: "second" })], true),
      "s2.b.0.0": tlResp([snapshotItem({ item_id: "s3.c.0.0", kind: "inbound_compact_summary", payload: "third" })], true),
      "s3.c.0.0": tlResp([snapshotItem({ item_id: "s4.d.0.0", kind: "inbound_compact_summary", payload: "fourth" })], false),
    };
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => Promise.resolve(opts?.before ? pages[opts.before] ?? tlResp([], false) : tlResp(postWindow(), true)));
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    await waitFor(() => expect(ctx.getQueryClient().getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(result.current.items.some((item) => item.item_id === "s4.d.0.0")).toBe(true));
    const beforeCalls = () => vi.mocked(api.getTimeline).mock.calls.map(([, opts]) => opts?.before).filter((before) => before !== undefined);
    expect(beforeCalls()).toEqual(["2.0", "1.5", "s1.a.0.0", "s2.b.0.0", "s3.c.0.0"]);
    act(() => useTimelineStore.setState((state) => ({ items: [...state.items, snapshotItem({ item_id: "3.0" })] })));
    await ctx.delay(20);
    expect(beforeCalls()).toHaveLength(5);
  });

  it("All waits for the post-compact tail read before deciding whether older history exists", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ settings: [
      { key: "display.compact_history_sessions", value: -1, updated_at: "2026-09-24T00:00:00Z" },
    ] });
    let postTailResolve: ((value: TimelineResponse) => void) | null = null;
    let postCompact = false;
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (opts?.before) return Promise.resolve(tlResp([
        snapshotItem({ item_id: "s1.a.1.0", payload: "retained" }),
      ], false));
      if (!postCompact) return Promise.resolve(tlResp([
        snapshotItem({ item_id: "90.0", payload: "before compact" }),
      ], false));
      return new Promise<TimelineResponse>((resolve) => { postTailResolve = resolve; });
    });
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("before compact"));
    await waitFor(() => expect(ctx.getQueryClient().getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());
    postCompact = true;
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(postTailResolve).not.toBeNull());
    expect(vi.mocked(api.getTimeline).mock.calls.filter(([, opts]) => opts?.before)).toHaveLength(0);
    act(() => { postTailResolve?.(tlResp(postWindow(), true)); });
    await waitFor(() => expect(result.current.items.some((item) => item.payload === "retained")).toBe(true));
    expect(vi.mocked(api.getTimeline).mock.calls.filter(([, opts]) => opts?.before)).toHaveLength(1);
  });

  it("All retries a failed history page and stops after the successful terminal page", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ settings: [
      { key: "display.compact_history_sessions", value: -1, updated_at: "2026-09-24T00:00:00Z" },
    ] });
    let olderAttempts = 0;
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (!opts?.before) return Promise.resolve(tlResp(postWindow(), true));
      olderAttempts += 1;
      if (olderAttempts === 1) return Promise.reject(new Error("transient"));
      return Promise.resolve(tlResp([snapshotItem({ item_id: "s1.a.1.0", payload: "recovered" })], false));
    });
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    await waitFor(() => expect(ctx.getQueryClient().getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(olderAttempts).toBe(1));
    act(() => useTimelineStore.setState((state) => ({ items: [...state.items, snapshotItem({ item_id: "3.0" })] })));
    await waitFor(() => expect(result.current.items.some((item) => item.payload === "recovered")).toBe(true));
    expect(olderAttempts).toBe(2);
    expect(showError).toHaveBeenCalledWith("Failed to load older messages: transient");
  });

  it("display.compact_history_sessions = 0 keeps the legacy clear-on-compact behavior (no retention fetch)", async () => {
    const showError = vi.fn();
    vi.mocked(api.getSettings).mockResolvedValue({
      settings: [
        { key: "display.compact_history_sessions", value: 0, updated_at: "2026-09-17T00:00:00Z" },
      ],
    });
    const postItems = [
      snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
      snapshotItem({ item_id: "1.0", kind: "agent_chat", payload: "post-compact" }),
    ];
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp(postItems, true));
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp([snapshotItem({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })], true),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    await waitFor(() => expect(ctx.getQueryClient().getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 2, items: postItems });
    await ctx.delay(20);

    const beforeCalls = vi
      .mocked(api.getTimeline)
      .mock.calls.filter((call) => call[1]?.before !== undefined);
    expect(beforeCalls).toHaveLength(0);
    expect(result.current.items.map((i) => i.item_id)).toEqual(["0.0", "1.0"]);
  });

  it("the post-compact snapshot also invalidates the agent's inspect / pending / token-usage caches (task #1959)", async () => {

    const invalidateSpy = ctx.spyInvalidateQueries();
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1));
    invalidateSpy.mockClear();

    pushEvent({ role: "compact_done", agent_id: 42 });
    await ctx.delay(0);
    expect(invalidateSpy).not.toHaveBeenCalled();

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 2,
      items: [snapshotItem({ item_id: "1.0", payload: "[summary]" })],
    });
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalled());
    const keys = invalidateSpy.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toEqual(
      expect.arrayContaining([
        ["timeline", 42],
        ["agent-inspect-live", 42],
        ["agent-inspect", 42],
        ["pending", 42],
        ["token-usage", 42],
      ]),
    );
  });
});

describe("useTimeline SSE inbound flow", () => {
  it("notes every event role that starts the timeline turn state", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    const starts: SystemEvent[] = [
      { role: "inbound_arrived", agent_id: 42, inbound_id: 1, kind: "chat", source: "user", content: "hi" },
      { role: "chat_start", agent_id: 42, item_id: "1.0" },
      { role: "code_start", agent_id: 42, item_id: "1.1" },
      { role: "reasoning_start", agent_id: 42, item_id: "1.2" },
      { role: "exec_start", agent_id: 42, item_id: "1.3" },
    ];
    for (const event of starts) pushEvent(event);

    expect(noteTurnStart).toHaveBeenCalledTimes(starts.length);
    expect(vi.mocked(noteTurnStart).mock.calls).toEqual(starts.map(() => [42]));
  });

  it("inbound_arrived no longer produces an optimistic placeholder — turnActive flips true immediately; items unchanged", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([
      snapshotItem({ kind: "code_output", payload: "previous" }),
    ]));

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

    expect(api.getTimeline).toHaveBeenCalledTimes(2);
  });

  it("timeline_snapshot push delivers envelope-wrap inbound_chat into the timeline", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ item_id: "1.0", kind: "code_output", payload: "previous" }),
    ]));

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
