import type * as ApiModule from "@/lib/transport/api";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineResponse } from "@/lib/contracts/types";
import { ApiError, type api } from "@/lib/transport/api";

import type { Viewport } from "../model/timeline-model";
import { useTimelineWindows } from "./use-timeline-windows";

const { getRunTimeline } = vi.hoisted(() => ({ getRunTimeline: vi.fn<typeof api.getRunTimeline>() }));
vi.mock("@/lib/transport/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  api: { getRunTimeline },
}));

const DAY = 86_400_000;
const END = Date.parse("2026-10-16T00:00:00Z");
const IDS = [42];
const first: RunTimelineResponse = {
  agent_id: 42,
  window: { from: new Date(END - DAY).toISOString(), to: new Date(END).toISOString() },
  lifetime: { from: new Date(END - 40 * DAY).toISOString(), to: new Date(END).toISOString() },
  nodes: [], units: [], units_omitted: 0, snapshot: "first",
};
const older = { from: END - 3 * DAY, to: END - 2 * DAY };
const response = (view: Viewport, snapshot = "first"): RunTimelineResponse => ({
  ...first, snapshot, window: { from: new Date(view.from).toISOString(), to: new Date(view.to).toISOString() },
});

const clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return renderHook(({ ids, view }) => useTimelineWindows(ids, view), {
    wrapper, initialProps: { ids: IDS, view: null as Viewport | null },
  });
}
afterEach(() => {
  vi.useRealTimers();
  clients.splice(0).forEach((client) => client.clear());
  getRunTimeline.mockReset();
});

describe("timeline viewport reads", () => {
  it("shows preparation and automatically retries only explicit pending states", async () => {
    getRunTimeline.mockRejectedValueOnce(new ApiError(503, "preparing", {
      detail: { code: "timeline_index_indexing", message: "preparing" },
    })).mockResolvedValue(first);
    const { result } = mount();
    await waitFor(() => expect(result.current.reads[0]?.preparing).toBe(true));
    expect(result.current.reads[0]?.isError).toBe(false);
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first), { timeout: 3000 });
    expect(result.current.reads[0]?.preparing).toBe(false);
    expect(getRunTimeline).toHaveBeenCalledTimes(2);
  });

  it("surfaces a failed preparation without retrying forever", async () => {
    getRunTimeline.mockRejectedValue(new ApiError(500, "failed", {
      detail: { code: "timeline_index_failed", message: "failed" },
    }));
    const { result } = mount();
    await waitFor(() => expect(result.current.reads[0]?.isError).toBe(true));
    expect(result.current.reads[0]?.preparing).toBe(false);
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("starts with one bounded day and reads older windows from the same snapshot", async () => {
    getRunTimeline.mockResolvedValueOnce(first).mockResolvedValue(response(older));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    expect(getRunTimeline).toHaveBeenCalledWith(42, { recentHours: 24, maxUnits: 5000, signal: expect.any(AbortSignal) as AbortSignal });
    rerender({ ids: IDS, view: { from: END - DAY / 2, to: END } });
    await act(() => new Promise((resolve) => setTimeout(resolve, 350)));
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data?.window).toEqual(response(older).window));
    expect(getRunTimeline).toHaveBeenLastCalledWith(42, {
      from: response(older).window.from, to: response(older).window.to,
      maxUnits: 5000, snapshot: "first", signal: expect.any(AbortSignal) as AbortSignal,
    });
  });

  it("coalesces a drag and aborts its pending request when the agent leaves", async () => {
    getRunTimeline.mockResolvedValueOnce(first).mockImplementation(() => new Promise(() => undefined));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: { from: END - 4 * DAY, to: END - 3 * DAY } });
    rerender({ ids: IDS, view: older });
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(2));
    expect(result.current.reads[0]?.pending).toBe(true);
    const signal = getRunTimeline.mock.calls[1][1]?.signal;
    expect(signal?.aborted).toBe(false);
    rerender({ ids: [], view: older });
    await waitFor(() => expect(signal?.aborted).toBe(true));
    expect(result.current.reads).toEqual([]);
  });

  it("refreshes an expired snapshot and retries the requested time range", async () => {
    getRunTimeline.mockResolvedValueOnce(first)
      .mockRejectedValueOnce(Object.assign(new Error("expired"), { status: 409 }))
      .mockResolvedValueOnce({ ...first, snapshot: "replacement" })
      .mockResolvedValue(response(older, "replacement"));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data?.snapshot).toBe("first"));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(older, "replacement")));
    expect(getRunTimeline).toHaveBeenCalledTimes(4);
    expect(getRunTimeline.mock.calls[3][1]?.snapshot).toBe("replacement");
  });

  it("keeps loaded data and exposes range failures for retry", async () => {
    getRunTimeline.mockResolvedValueOnce(first).mockRejectedValue(new Error("offline"));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.windowError).toBe(true));
    expect(result.current.reads[0]?.data).toEqual(first);
    getRunTimeline.mockResolvedValue(response(older));
    act(() => result.current.retry(42));
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(older)));
  });

  it("keeps the last range through another pan, debounce, failure and retry", async () => {
    const next = { from: older.from - DAY / 2, to: older.to - DAY / 2 };
    let reject: (error: Error) => void = () => undefined;
    getRunTimeline.mockResolvedValueOnce(first).mockResolvedValueOnce(response(older))
      .mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(older)));

    rerender({ ids: IDS, view: next });
    expect(result.current.reads[0]?.data).toEqual(response(older));
    expect(result.current.reads[0]?.pending).toBe(true);
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(3));
    expect(result.current.reads[0]?.data).toEqual(response(older));
    act(() => reject(new Error("offline")));
    await waitFor(() => expect(result.current.reads[0]?.windowError).toBe(true));
    expect(result.current.reads[0]?.data).toEqual(response(older));

    getRunTimeline.mockResolvedValue(response(next));
    act(() => result.current.retry(42));
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(next)));
    rerender({ ids: IDS, view: { from: END - DAY / 2, to: END } });
    expect(result.current.reads[0]?.data).toEqual(first);
    expect(result.current.reads[0]?.pending).toBe(false);
  });

  it("never retains a range from a replaced snapshot or another agent", async () => {
    const next = { from: older.from - DAY, to: older.to - DAY };
    getRunTimeline.mockResolvedValueOnce(first).mockResolvedValueOnce(response(older))
      .mockRejectedValueOnce(Object.assign(new Error("expired"), { status: 409 }))
      .mockResolvedValueOnce({ ...first, snapshot: "replacement" })
      .mockImplementation(() => new Promise(() => undefined));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(older)));
    rerender({ ids: IDS, view: next });
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(5));
    expect(result.current.reads[0]?.data).toEqual({ ...first, snapshot: "replacement" });

    const other = { ...first, agent_id: 99 };
    getRunTimeline.mockResolvedValueOnce(other);
    rerender({ ids: [99], view: null });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(other));
    rerender({ ids: [99], view: older });
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(7));
    expect(result.current.reads[0]?.data).toEqual(other);
  });

  it("retains the displayed cached window rather than the last downloaded window", async () => {
    const second = { from: older.from - DAY, to: older.to - DAY };
    const third = { from: second.from - DAY, to: second.to - DAY };
    getRunTimeline.mockResolvedValueOnce(first).mockResolvedValueOnce(response(older))
      .mockResolvedValueOnce(response(second)).mockImplementation(() => new Promise(() => undefined));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(older)));
    rerender({ ids: IDS, view: second });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(second)));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(older)));
    expect(getRunTimeline).toHaveBeenCalledTimes(3);
    rerender({ ids: IDS, view: third });
    expect(result.current.reads[0]?.data).toEqual(response(older));
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(4));
    expect(result.current.reads[0]?.data).toEqual(response(older));
  });

  it("keeps a displayed range alive past cache GC while a later range fails", async () => {
    const next = { from: older.from - DAY, to: older.to - DAY };
    getRunTimeline.mockResolvedValueOnce(first).mockResolvedValueOnce(response(older))
      .mockRejectedValue(new Error("offline"));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(response(older)));
    vi.useFakeTimers();
    rerender({ ids: IDS, view: next });
    await act(() => vi.advanceTimersByTimeAsync(350));
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(result.current.reads[0]?.windowError).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(60_001));
    rerender({ ids: IDS, view: { from: next.from - 1000, to: next.to - 1000 } });
    expect(result.current.reads[0]?.data).toEqual(response(older));
    await act(() => vi.advanceTimersByTimeAsync(350));
    expect(result.current.reads[0]?.data).toEqual(response(older));
  });
});
