import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  TimelineResponse,
} from "../../contracts/types";
import { useTimeline } from "../use-timeline";
import { registerUseTimelineCompactTests } from "./use-timeline-compact";
import type { UseTimelineTestContext } from "./use-timeline-context";
import { registerUseTimelineOwnershipTests } from "./use-timeline-ownership";
import { registerUseTimelineSseTests } from "./use-timeline-sse";
import { registerUseTimelineStreamingTests } from "./use-timeline-streaming";
import { registerUseTimelineSwitchTests } from "./use-timeline-switch";

export function registerUseTimelinePaginationTests(ctx: UseTimelineTestContext): void {
  const {
    ApiError,
    timeline,
    timelineReads,
    resolves,
    rejects,
    snapshotItem,
    tlResp,
    wrapper,
  } = ctx;

describe("useTimeline mount + pagination", () => {
  it("loadOlder skips the re-attached standing head notes as cursors", async () => {

    const showError = vi.fn();
    timeline.queue(resolves(
      tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({
            item_id: "1.0",
            kind: "system_marker",
            source: "exec_timeout",
            payload: "timeout",
          }),
          snapshotItem({
            item_id: "2.0",
            kind: "system_marker",
            source: "timezone",
            payload: "tz",
          }),
          snapshotItem({
            item_id: "3.0",
            kind: "system_marker",
            source: "memory",
            payload: "cluster memory",
          }),
          snapshotItem({
            item_id: "4.0",
            kind: "system_marker",
            source: "agent_id",
            payload: "id",
          }),
          snapshotItem({
            item_id: "5.0",
            kind: "system_marker",
            source: "agent_memory",
            payload: "agent memory",
          }),
          snapshotItem({
            item_id: "6.0",
            kind: "inbound_compact_summary",
            payload: "SUMMARY",
          }),
          snapshotItem({ item_id: "960.1", kind: "inbound_chat", payload: "recent" }),
        ],
        true,
      ),
    ));
    timeline.queue(resolves(
      tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({ item_id: "915.1", kind: "agent_chat", payload: "older" }),
        ],
        false,
      ),
    ));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(8));
    expect(result.current.hasMoreOlder).toBe(true);
    timelineReads.length = 0;

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.items).toHaveLength(9));
    expect(timelineReads).toEqual([{
      agentId: 42,
      before: "960.1",
      limit: 50,
      signal: expect.any(AbortSignal) as AbortSignal,
    }]);
    expect(result.current.items.map((i) => i.item_id)).toEqual([
      "0.0",
      "1.0",
      "2.0",
      "3.0",
      "4.0",
      "5.0",
      "6.0",
      "915.1",
      "960.1",
    ]);
    expect(showError).not.toHaveBeenCalled();
  });

  it("loadOlder is a no-op when hasMoreOlder is false", async () => {
    const showError = vi.fn();
    timeline.queue(resolves(
      tlResp([snapshotItem({ item_id: "1.0", kind: "inbound_chat", payload: "only" })], false),
    ));
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    expect(result.current.hasMoreOlder).toBe(false);

    timelineReads.length = 0;
    act(() => result.current.loadOlder());
    expect(timelineReads).toHaveLength(0);
  });

  it("loadOlder uses the complete historical item id as its cursor", async () => {
    const showError = vi.fn();
    const historicalId = "s2.1f0b9b12-0000-6000-8000-000000000000.14.1";
    timeline.queue(
      resolves(
        tlResp(
          [snapshotItem({ item_id: historicalId, kind: "agent_chat", payload: "old" })],
          true,
        ),
      ),
      resolves(tlResp([], false)),
    );
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    timelineReads.length = 0;

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.hasMoreOlder).toBe(false));
    expect(timelineReads).toContainEqual({
      agentId: 42,
      before: historicalId,
      limit: 50,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
  });

  it("uses a historical compact summary as a bounded continuation cursor", async () => {
    const showError = vi.fn();
    const summaryId = "s1.1f0b9b12-0000-6000-8000-000000000000.0.0";
    timeline.queue(
      resolves(
        tlResp(
          [
            snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
            snapshotItem({
              item_id: "1.0",
              kind: "inbound_compact_summary",
              payload: "CURRENT SUMMARY",
            }),
            snapshotItem({ item_id: "2.0", kind: "agent_chat", payload: "current" }),
            snapshotItem({
              item_id: summaryId,
              kind: "inbound_compact_summary",
              payload: "HISTORICAL SUMMARY ONLY",
            }),
          ],
          true,
        ),
      ),
      resolves(tlResp([], false)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.hasMoreOlder).toBe(true));
    timelineReads.length = 0;

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.hasMoreOlder).toBe(false));
    expect(timelineReads).toContainEqual({
      agentId: 42,
      before: summaryId,
      limit: 50,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
  });

  it("getTimeline failure calls showError; items unchanged", async () => {
    const showError = vi.fn();
    timeline.always(rejects(new Error("boom")));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });

    await waitFor(() => {
      expect(showError).toHaveBeenCalled();
    });
    expect(showError.mock.calls[0][0]).toContain("Failed to load timeline");
    expect(result.current.items).toEqual([]);
  });

  it("exposes a cold 503 and retries the authoritative timeline without accepting empty success", async () => {
    const failure = new ApiError(503, "Checkpoint history unavailable for agent 42");
    timeline.queue(rejects(failure));
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.error).toBe(failure));
    expect(result.current.isLoading).toBe(false);
    expect(ctx.getQueryClient().getQueryData(["timeline", 42])).toBeUndefined();
    expect(result.current.items).toEqual([]);

    timeline.queue(resolves(tlResp([
      snapshotItem({ kind: "agent_chat", payload: "read restored" }),
    ])));
    act(() => result.current.retryTimeline());
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("read restored"));
    expect(result.current.error).toBeNull();
  });

  it("keeps loaded history when a refresh returns 503 and during its pending manual retry", async () => {
    timeline.queue(resolves(tlResp([
      snapshotItem({ kind: "agent_chat", payload: "retained history" }),
    ])));
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("retained history"));
    const failure = new ApiError(503, "Checkpoint history unavailable for agent 42");
    timeline.queue(rejects(failure));
    act(() => result.current.retryTimeline());
    await waitFor(() => expect(result.current.error).toBe(failure));
    expect(result.current.items[0]?.payload).toBe("retained history");
    let complete: ((page: TimelineResponse) => void) | undefined;
    timeline.queue(() => new Promise((resolve) => { complete = resolve; }));
    act(() => result.current.retryTimeline());
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.items[0]?.payload).toBe("retained history");
    await act(async () => {
      complete?.(tlResp([
        snapshotItem({ kind: "agent_chat", payload: "fresh history" }),
      ]));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("fresh history"));
    expect(result.current.error).toBeNull();
  });
});

  registerUseTimelineSwitchTests(ctx);
  registerUseTimelineCompactTests(ctx);
  registerUseTimelineSseTests(ctx);
  registerUseTimelineStreamingTests(ctx);
  registerUseTimelineOwnershipTests(ctx);
}
