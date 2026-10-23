import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  TimelineResponse,
} from "../../contracts/types";
import { useTimeline } from "../use-timeline";
import type { UseTimelineTestContext } from "./use-timeline-context";

export function registerUseTimelineSwitchTests(ctx: UseTimelineTestContext): void {
  const {
    api,
    pushEvent,
    pushConnectionEvent,
    snapshotItem,
    postWindow,
    tlResp,
    composedSnapshot,
    wrapper,
  } = ctx;

describe("useTimeline agentId switch", () => {
  it("agentId changes → items cleared immediately → fetches new snapshot", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "thread-A" })]))
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "thread-B" })]));

    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );

    await waitFor(() => expect(result.current.items[0]?.payload).toBe("thread-A"));

    rerender({ tid: 2 });

    await waitFor(() => expect(result.current.items[0]?.payload).toBe("thread-B"));
    expect(api.getTimeline).toHaveBeenLastCalledWith(2, expect.objectContaining({ signal: expect.any(AbortSignal) as AbortSignal }));
    unmount();
  });

  it("refetch within the same agent (a NEW response object) still calls reloadSnapshot to update items", async () => {

    const showError = vi.fn();
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "v1" })]))
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "v2" })]));

    const { result, unmount } = renderHook(() => useTimeline(42, showError), { wrapper });

    await waitFor(() => expect(result.current.items[0]?.payload).toBe("v1"));
    expect(api.getTimeline).toHaveBeenCalledTimes(1);

    await act(async () => {
      await ctx.getQueryClient().invalidateQueries({ queryKey: ["timeline", 42] });
    });

    await waitFor(() => expect(result.current.items[0]?.payload).toBe("v2"));
    expect(api.getTimeline).toHaveBeenCalledTimes(2);

    unmount();
  });

  it("on agent switch, thread guard prevents wrong-thread events from contaminating the new thread", async () => {

    const showError = vi.fn();
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(tlResp([]))
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "thread-B fresh" })]));

    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );

    await waitFor(() => expect(result.current.items).toEqual([]));

    pushEvent({ role: "chat_start", item_id: "5.0", agent_id: 1 });

    rerender({ tid: 2 });

    await waitFor(() =>
      expect(result.current.items[0]?.payload).toBe("thread-B fresh"),
    );

    unmount();
    expect(result.current.items.find((i) => i.item_id === "5.0")).toBeUndefined();
  });

  it("activation seeds a retained snapshot; the reattach reconcile refreshes it", async () => {
    const showError = vi.fn();

    ctx.getQueryClient().setQueryData<TimelineResponse>(
      ["timeline", 7],
      tlResp([snapshotItem({ item_id: "1.0", payload: "stale cached" })]),
    );

    vi.mocked(api.getConversationSnapshot).mockReset().mockResolvedValue(
      composedSnapshot(tlResp([
        snapshotItem({ item_id: "1.0", payload: "fresh" }),
        snapshotItem({ item_id: "2.0", payload: "happened while away" }),
      ])),
    );

    const { result, unmount } = renderHook(() => useTimeline(7, showError), { wrapper });

    await waitFor(() =>
      expect(result.current.items.map((i) => i.payload)).toEqual(["stale cached"]),
    );
    expect(api.getTimeline).not.toHaveBeenCalled();

    pushConnectionEvent({ type: "open" });
    await waitFor(() =>
      expect(result.current.items.map((i) => i.payload)).toEqual(["fresh", "happened while away"]),
    );
    expect(api.getConversationSnapshot).toHaveBeenCalledWith(7, expect.any(AbortSignal) as AbortSignal);
    unmount();
  });

  it("compact_done for a NON-active thread does not refetch the active thread (targets ev.agent_id)", async () => {

    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1));

    pushEvent({ role: "compact_done", agent_id: 99 });

    await ctx.delay(0);
    expect(api.getTimeline).toHaveBeenCalledTimes(1);
  });

  it("compact_done defers the refetch until a nonempty timeline snapshot arrives", async () => {

    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1));

    pushEvent({ role: "compact_done", agent_id: 42 });
    await ctx.delay(0);
    expect(api.getTimeline).toHaveBeenCalledTimes(1);

    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 0, items: [] });
    await ctx.delay(0);
    expect(api.getTimeline).toHaveBeenCalledTimes(1);

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 2,
      items: [snapshotItem({ item_id: "1.0", payload: "[summary]" })],
    });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(2));

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 3,
      items: [snapshotItem({ item_id: "1.0", payload: "[summary]" })],
    });
    await ctx.delay(0);
    expect(api.getTimeline).toHaveBeenCalledTimes(2);
  });

  it("after a compact reset, re-attaches the previous session (default retention = 1)", async () => {

    const showError = vi.fn();
    let phase: "pre" | "post" = "pre";
    const pageFor = (opts?: { before?: string; limit?: number }): TimelineResponse => {
      if (opts?.before === "2.0") {
        return tlResp(
          [
            snapshotItem({
              item_id: "s1.boundary.0.0",
              kind: "inbound_compact_summary",
              payload: "prev summary",
            }),
            snapshotItem({
              item_id: "s1.boundary.7.0",
              kind: "agent_chat",
              payload: "previous session",
            }),
          ],
          false,
        );
      }
      if (opts?.before !== undefined) return tlResp([], false);
      if (phase === "pre") {
        return tlResp(
          [snapshotItem({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })],
          true,
        );
      }
      return tlResp(postWindow(), true);
    };
    vi.mocked(api.getTimeline).mockImplementation((_id: number, opts?: { before?: string; limit?: number }) =>
      Promise.resolve(pageFor(opts)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    expect(result.current.hasMoreOlder).toBe(true);

    phase = "post";
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 3,
      items: postWindow(),
    });

    await waitFor(() => {
      expect(api.getTimeline).toHaveBeenCalledWith(42, { before: "2.0", limit: 50, signal: expect.any(AbortSignal) as AbortSignal });
    });
    await waitFor(() => {
      expect(result.current.items.map((i) => i.item_id)).toEqual([
        "s1.boundary.0.0",
        "s1.boundary.7.0",
        "0.0",
        "1.0",
        "2.0",
      ]);
    });
    expect(showError).not.toHaveBeenCalled();
  });

  it("head-only post-compact segment crosses from the summary cursor (no real item yet)", async () => {

    const showError = vi.fn();
    let phase: "pre" | "post" = "pre";
    const pageFor = (opts?: { before?: string; limit?: number }): TimelineResponse => {
      if (opts?.before === "1.0") {
        return tlResp(
          [
            snapshotItem({
              item_id: "s1.boundary.7.0",
              kind: "agent_chat",
              payload: "previous session",
            }),
          ],
          false,
        );
      }
      if (opts?.before !== undefined) return tlResp([], false);
      if (phase === "pre") {
        return tlResp(
          [snapshotItem({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })],
          true,
        );
      }
      return tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({ item_id: "1.0", kind: "inbound_compact_summary", payload: "SUMMARY" }),
        ],
        true,
      );
    };
    vi.mocked(api.getTimeline).mockImplementation((_id: number, opts?: { before?: string; limit?: number }) =>
      Promise.resolve(pageFor(opts)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    phase = "post";
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 2,
      items: [
        snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
        snapshotItem({ item_id: "1.0", kind: "inbound_compact_summary", payload: "SUMMARY" }),
      ],
    });

    await waitFor(() => {
      expect(api.getTimeline).toHaveBeenCalledWith(42, { before: "1.0", limit: 50, signal: expect.any(AbortSignal) as AbortSignal });
    });
    await waitFor(() => {
      expect(result.current.items.map((i) => i.item_id)).toEqual([
        "s1.boundary.7.0",
        "0.0",
        "1.0",
      ]);
    });
  });

});
}
