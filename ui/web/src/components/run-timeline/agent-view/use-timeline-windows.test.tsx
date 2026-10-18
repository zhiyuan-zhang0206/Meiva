import type * as ApiModule from "@/lib/transport/api";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
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

const distant = { from: END - 10 * DAY, to: END - 9 * DAY };
function windowResponse(id: number, options: Parameters<typeof api.getRunTimeline>[1]): RunTimelineResponse {
  if (!options?.from || !options.to) throw new Error("expected a bounded window");
  const idx = Math.round((END - Date.parse(options.from)) / DAY);
  return {
    ...first, agent_id: id, window: { from: options.from, to: options.to },
    units: [{ kind: "text", i0: idx, i1: idx, start: options.from, end: options.to,
      source: null, inbound_id: null, preview: `bar ${idx}`, parent: null, context_tokens: 10,
      generation_tokens: null, estimated: false, session: 0, context_total: 20, request: null }],
  };
}

const clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const wrapper = ({ children }: { children: ReactNode }) => <StrictMode><QueryClientProvider client={client}>{children}</QueryClientProvider></StrictMode>;
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

  it("prefetches neighboring ranges and reuses their union on non-exact return visits", async () => {
    getRunTimeline.mockResolvedValueOnce(first).mockImplementation((id, options) => Promise.resolve(windowResponse(id, options)));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    expect(getRunTimeline.mock.calls[0][1]).toMatchObject({ recentHours: 24, maxUnits: 5000 });
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data?.units).toHaveLength(1));
    expect(getRunTimeline.mock.calls[1][1]).toMatchObject({
      from: new Date(END - 4 * DAY).toISOString(), to: first.window.from, snapshot: "first",
    });
    const retained = result.current.reads[0]?.data?.units;
    const calls = getRunTimeline.mock.calls.length;
    rerender({ ids: IDS, view: { from: older.from - DAY / 4, to: older.to - DAY / 4 } });
    expect(result.current.reads[0]?.data?.units).toEqual(retained);
    expect(result.current.reads[0]?.pending).toBe(false);
    await act(() => new Promise((resolve) => setTimeout(resolve, 120)));
    expect(getRunTimeline).toHaveBeenCalledTimes(calls);
    rerender({ ids: IDS, view: { from: END - DAY / 2, to: END } });
    expect(result.current.reads[0]?.data?.units).toEqual(retained);
    expect(result.current.reads[0]?.pending).toBe(false);
  });

  it("starts reads during continuous movement and keeps them alive through further movement", async () => {
    getRunTimeline.mockResolvedValueOnce(first).mockImplementation(() => new Promise(() => undefined));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    await act(() => new Promise((resolve) => setTimeout(resolve, 100)));
    vi.useFakeTimers();
    for (let step = 0; step < 10; step++) {
      rerender({ ids: IDS, view: { from: older.from - step * 1000, to: older.to - step * 1000 } });
      await act(() => vi.advanceTimersByTimeAsync(20));
    }
    expect(getRunTimeline).toHaveBeenCalledTimes(2);
    const signal = getRunTimeline.mock.calls[1][1]?.signal;
    expect(signal?.aborted).toBe(false);
    expect(result.current.reads[0]?.pending).toBe(true);
    rerender({ ids: [], view: older });
    expect(signal?.aborted).toBe(true);
  });

  it("refreshes an expired snapshot and excludes all bars belonging to the old snapshot", async () => {
    getRunTimeline.mockResolvedValueOnce(first)
      .mockImplementationOnce((id, options) => Promise.resolve(windowResponse(id, options)))
      .mockRejectedValueOnce(Object.assign(new Error("expired"), { status: 409 }))
      .mockResolvedValueOnce({ ...first, snapshot: "replacement" })
      .mockImplementation(() => new Promise(() => undefined));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data?.units).toHaveLength(1));
    rerender({ ids: IDS, view: distant });
    await waitFor(() => expect(result.current.reads[0]?.data?.snapshot).toBe("replacement"));
    expect(result.current.reads[0]?.data?.units).toEqual([]);
    await waitFor(() => expect(getRunTimeline.mock.calls.at(-1)?.[1]?.snapshot).toBe("replacement"));
    const other = { ...first, agent_id: 99 };
    getRunTimeline.mockResolvedValueOnce(other);
    rerender({ ids: [99], view: null });
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(other));
  });

  it("retains all visited bars through a failed read, cache GC and a retry", async () => {
    getRunTimeline.mockResolvedValueOnce(first)
      .mockImplementationOnce((id, options) => Promise.resolve(windowResponse(id, options)))
      .mockRejectedValue(new Error("offline"));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: older });
    await waitFor(() => expect(result.current.reads[0]?.data?.units).toHaveLength(1));
    const bars = result.current.reads[0]?.data?.units;
    rerender({ ids: IDS, view: distant });
    await waitFor(() => expect(result.current.reads[0]?.windowError).toBe(true));
    vi.useFakeTimers();
    await act(() => vi.advanceTimersByTimeAsync(60_001));
    expect(result.current.reads[0]?.data?.units).toEqual(bars);
    rerender({ ids: IDS, view: older });
    expect(result.current.reads[0]?.pending).toBe(false);
    expect(result.current.reads[0]?.windowError).toBe(false);
    expect(result.current.reads[0]?.data?.units).toEqual(bars);
    rerender({ ids: IDS, view: distant });
    getRunTimeline.mockImplementation((id, options) => Promise.resolve(windowResponse(id, options)));
    act(() => result.current.retry(42));
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(result.current.reads[0]?.windowError).toBe(false);
    expect(result.current.reads[0]?.data?.units).toHaveLength(2);
    expect(getRunTimeline).toHaveBeenCalledTimes(4);
  });

  it("fetches gaps between cached ranges instead of treating their hull as complete", async () => {
    getRunTimeline.mockResolvedValueOnce(first).mockImplementation((id, options) => Promise.resolve(windowResponse(id, options)));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view: distant });
    await waitFor(() => expect(result.current.reads[0]?.data?.units).toHaveLength(1));
    rerender({ ids: IDS, view: older });
    expect(result.current.reads[0]?.pending).toBe(true);
    await waitFor(() => expect(result.current.reads[0]?.data?.units).toHaveLength(2));
    expect(result.current.reads[0]?.pending).toBe(false);
  });

  it.each([0, 0.8])("recovers dense detail with outward-rounded millisecond bounds (offset %s)", async (offset) => {
    const view = { from: older.from + offset, to: older.to + offset };
    getRunTimeline.mockResolvedValueOnce(first)
      .mockImplementationOnce((id, options) => Promise.resolve({ ...windowResponse(id, options), units: [], units_omitted: 6000 }))
      .mockImplementation((id, options) => Promise.resolve(windowResponse(id, options)));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(first));
    rerender({ ids: IDS, view });
    await waitFor(() => expect(result.current.reads[0]?.data?.units).toHaveLength(1));
    expect(getRunTimeline.mock.calls[2][1]).toMatchObject(response({ from: Math.floor(view.from), to: Math.ceil(view.to) }).window);
    expect(result.current.reads[0]?.data?.units_omitted).toBe(0);
    expect(getRunTimeline).toHaveBeenCalledTimes(3);
  });
  it("cancels a removed agent even when remaining agents share a null snapshot", async () => {
    getRunTimeline.mockImplementation((id, options) => options?.recentHours
      ? Promise.resolve({ ...first, agent_id: id, snapshot: null })
      : new Promise(() => undefined));
    const { result, rerender } = mount();
    rerender({ ids: [42, 99], view: older });
    await waitFor(() => expect(getRunTimeline.mock.calls.filter(([, options]) => options?.from)).toHaveLength(2));
    const removed = getRunTimeline.mock.calls.find(([id, options]) => id === 42 && options?.from)?.[1]?.signal;
    const remaining = getRunTimeline.mock.calls.find(([id, options]) => id === 99 && options?.from)?.[1]?.signal;
    rerender({ ids: [99], view: older });
    await waitFor(() => expect(removed?.aborted).toBe(true));
    expect(remaining?.aborted).toBe(false);
    expect(result.current.reads).toHaveLength(1);
    expect(result.current.reads[0]?.data?.agent_id).toBe(99);
  });

  it("reuses dense recovery objects and requests only newly exposed edges", async () => {
    const dense = { ...first, units_omitted: 6000 };
    getRunTimeline.mockImplementation((id, options) => Promise.resolve(options?.recentHours ? dense : windowResponse(id, options)));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.reads[0]?.data).toEqual(dense));
    const view = { from: END - DAY / 2, to: END - DAY / 4 };
    rerender({ ids: IDS, view });
    await waitFor(() => expect(result.current.reads[0]?.data?.units_omitted).toBe(0));
    const display = result.current.reads[0]?.data;
    rerender({ ids: IDS, view: { from: view.from + 1000, to: view.to - 1000 } });
    expect(result.current.reads[0]?.data).toBe(display);
    await act(() => new Promise((resolve) => setTimeout(resolve, 120)));
    expect(result.current.reads[0]?.data).toBe(display);
    expect(getRunTimeline).toHaveBeenCalledTimes(2);
    rerender({ ids: IDS, view: { from: view.from - 1000, to: view.to - 1000 } });
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(3));
    expect(getRunTimeline.mock.calls[2][1]).toMatchObject({
      from: new Date(view.from - 1000).toISOString(), to: new Date(view.from).toISOString(),
    });
    await waitFor(() => expect(result.current.reads[0]?.pending).toBe(false));
    expect(result.current.reads[0]?.data?.units_omitted).toBe(0);
  });

});
