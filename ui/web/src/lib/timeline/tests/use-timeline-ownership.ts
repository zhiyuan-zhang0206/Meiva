import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SETTINGS_QUERY_KEY } from "../../state/use-user-settings";

import type {
  ConversationSnapshotResponse,
  TimelineResponse,
} from "../../contracts/types";
import { useTimelineStore } from "../timeline-store";
import { useTimeline } from "../use-timeline";
import type { UseTimelineTestContext } from "./use-timeline-context";

export function registerUseTimelineOwnershipTests(ctx: UseTimelineTestContext): void {
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

describe("selected timeline ownership", () => {
  it("clearing selection releases displayed content and starts no request", async () => {
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([snapshotItem({ payload: "selected" })]));
    const { result, rerender } = renderHook<ReturnType<typeof useTimeline>, { id: number | null }>(({ id }) => useTimeline(id, vi.fn()), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("selected"));
    const reads = vi.mocked(api.getTimeline).mock.calls.length;
    rerender({ id: null });
    expect(result.current.items).toEqual([]);
    expect(useTimelineStore.getState().activeThreadId).toBeNull();
    expect(api.getTimeline).toHaveBeenCalledTimes(reads);
  });

  it("rapid A to B to A aborts obsolete reads and cannot accept the first A response", async () => {
    const reads: { id: number; signal: AbortSignal; resolve: (data: TimelineResponse) => void }[] = [];
    vi.mocked(api.getTimeline).mockImplementation((id, opts) => new Promise((resolve) => {
      if (!opts?.signal) throw new Error("missing AbortSignal");
      reads.push({ id, signal: opts.signal, resolve });
    }));
    const { result, rerender } = renderHook(({ id }: { id: number }) => useTimeline(id, vi.fn()), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(reads).toHaveLength(1));
    rerender({ id: 2 });
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(reads[0].signal.aborted).toBe(true);
    rerender({ id: 1 });
    await waitFor(() => expect(reads).toHaveLength(3));
    expect(reads[1].signal.aborted).toBe(true);
    await act(async () => {
      reads[2].resolve(tlResp([snapshotItem({ item_id: "1.0", payload: "current A" })]));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("current A"));
    await act(async () => {
      reads[0].resolve(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete A" })]));
      reads[1].resolve(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete B" })]));
      await Promise.resolve();
    });
    expect(result.current.items.map((item) => item.payload)).toEqual(["current A"]);
  });

  it("aborts old history pages even after switching back to the same agent", async () => {
    let finishPage!: (data: TimelineResponse) => void;
    let pageSignal: AbortSignal | undefined;
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (opts?.before) {
        pageSignal = opts.signal;
        return new Promise((resolve) => { finishPage = resolve; });
      }
      return Promise.resolve(tlResp([snapshotItem({ item_id: "5.0", payload: "current" })], true));
    });
    const { result, rerender } = renderHook(({ id }: { id: number }) => useTimeline(id, showError), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(result.current.hasMoreOlder).toBe(true));
    act(() => result.current.loadOlder());
    rerender({ id: 2 });
    expect(pageSignal?.aborted).toBe(true);
    rerender({ id: 1 });
    await act(async () => {
      finishPage(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete page" })]));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.items.map((item) => item.payload)).toEqual(["current"]));
    expect(showError).not.toHaveBeenCalled();
  });

  it.each(["hidden", "compact"])("%s abandons a history request without accepting its late response", async (cause) => {
    let finishPage!: (data: TimelineResponse) => void;
    let pageSignal: AbortSignal | undefined;
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (opts?.before) {
        pageSignal = opts.signal;
        return new Promise((resolve) => { finishPage = resolve; });
      }
      return Promise.resolve(tlResp([snapshotItem({ item_id: "5.0", payload: "current" })], true));
    });
    const { result } = renderHook(() => useTimeline(1, showError), { wrapper });
    await waitFor(() => expect(result.current.hasMoreOlder).toBe(true));
    act(() => result.current.loadOlder());
    expect(result.current.loadingOlder).toBe(true);
    const visibility = ctx.spyDocumentVisibility();
    try {
      if (cause === "hidden") {
        act(() => {
          visibility.mockReturnValue("hidden");
          ctx.dispatchVisibilityChange();
        });
      } else {
        pushEvent({ role: "compact_done", agent_id: 1 });
      }
      expect(pageSignal?.aborted).toBe(true);
      expect(result.current.loadingOlder).toBe(false);
      await act(async () => {
        finishPage(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete page" })]));
        await Promise.resolve();
      });
      expect(result.current.items.map((item) => item.payload)).toEqual(["current"]);
      expect(showError).not.toHaveBeenCalled();
    } finally {
      visibility.mockRestore();
    }
  });

  it("returning after an inactive compact refreshes to the authoritative tail on reattach", async () => {
    let compacted = false;
    vi.mocked(api.getTimeline).mockImplementation((id) => Promise.resolve(
      tlResp([snapshotItem({ item_id: "1.0", payload: id === 1 && compacted ? "compacted A" : `agent ${id}` })]),
    ));
    vi.mocked(api.getConversationSnapshot).mockReset().mockImplementation(() =>
      Promise.resolve(composedSnapshot(tlResp([
        snapshotItem({ item_id: "1.0", payload: "compacted A" }),
      ]))));
    const { result, rerender } = renderHook(({ id }: { id: number }) => useTimeline(id, vi.fn()), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("agent 1"));
    rerender({ id: 2 });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("agent 2"));
    compacted = true;
    pushEvent({ role: "chat_delta", agent_id: 1, item_id: "8.0", content: "late old A" });
    rerender({ id: 1 });

    await waitFor(() => expect(result.current.items.map((item) => item.payload)).toEqual(["agent 1"]));
    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(result.current.items.map((item) => item.payload)).toEqual(["compacted A"]));

    expect(api.getTimeline).toHaveBeenCalledTimes(2);
    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);

    expect(ctx.getQueryClient().getQueryCache().findAll({ queryKey: ["timeline"] })).toHaveLength(2);
  });
});

describe("selected opening-gap reconcile", () => {
  it("reads again after a pre-open initial read settles", async () => {
    let finish!: (value: TimelineResponse) => void;
    vi.mocked(api.getTimeline)
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValue(tlResp([snapshotItem({ item_id: "1.0", payload: "unexpected per-domain read" })]));
    vi.mocked(api.getConversationSnapshot).mockReset().mockResolvedValue(
      composedSnapshot(tlResp([snapshotItem({ item_id: "1.0", payload: "committed before stream opened" })])),
    );
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1));
    pushConnectionEvent({ type: "open" });
    await act(async () => {
      finish(tlResp([snapshotItem({ item_id: "1.0", payload: "read before subscription" })]));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("committed before stream opened"));

    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);
    expect(api.getTimeline).toHaveBeenCalledTimes(1);
  });

  it("compact_done aborts an in-flight reconcile: the pre-compact snapshot never lands", async () => {
    vi.mocked(api.getTimeline).mockResolvedValue(
      tlResp([snapshotItem({ item_id: "1.0", payload: "stable" })]),
    );
    let finishSnapshot!: (value: ConversationSnapshotResponse) => void;
    vi.mocked(api.getConversationSnapshot).mockReset().mockImplementation(
      () => new Promise((resolve) => { finishSnapshot = resolve; }),
    );
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("stable"));

    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1));

    pushEvent({ role: "compact_done", agent_id: 42 });

    await act(async () => {
      finishSnapshot(composedSnapshot(
        tlResp([snapshotItem({ item_id: "1.0", payload: "pre-compact ghost" })]),
      ));
      await Promise.resolve();
    });
    expect(result.current.items.map((item) => item.payload)).toEqual(["stable"]);
    expect(ctx.getQueryClient().getQueryData<TimelineResponse>(["timeline", 42]))
      .toMatchObject({ items: [{ payload: "stable" }] });
  });
});

describe("compact retention request ownership", () => {
  it("a reconnect-only compact replacement reattaches the previous session once", async () => {
    let compacted = false;
    const tailWindow = () => tlResp(compacted ? postWindow() : [
      snapshotItem({ item_id: "90.0", payload: "before gap" }),
    ], true);
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (opts?.before) return Promise.resolve(tlResp([
        snapshotItem({ item_id: "s1.gap.1.0", payload: "retained across reconnect" }),
      ], false));
      return Promise.resolve(tailWindow());
    });
    vi.mocked(api.getConversationSnapshot).mockReset().mockImplementation(() =>
      Promise.resolve(composedSnapshot(tailWindow())));
    const { result } = renderHook(() => useTimeline(1, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("before gap"));
    compacted = true;
    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(result.current.items.map((item) => item.payload)).toContain("retained across reconnect"));
    expect(useTimelineStore.getState().compactReplaceSeq).toBe(1);
    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(api.getConversationSnapshot).toHaveBeenCalledTimes(2));
    expect(useTimelineStore.getState().compactReplaceSeq).toBe(1);

    expect(api.getTimeline).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.getTimeline).mock.calls.filter((call) => call[1]?.before)).toHaveLength(1);
  });

  it.each(["switch", "hidden", "unmount", "clear"].flatMap((cause) => [2, -1].map((retention) => ({ cause, retention }))))("$cause cancels retention $retention and rejects its late completion", async ({ cause, retention }) => {
    const pages: { signal: AbortSignal; resolve: (page: TimelineResponse) => void }[] = [];
    const showError = vi.fn();
    vi.mocked(api.getSettings).mockResolvedValue({ settings: [
      { key: "display.compact_history_sessions", value: retention, updated_at: "2026-09-17T00:00:00Z" },
    ] });
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (!opts?.before) return Promise.resolve(tlResp(postWindow(), true));
      if (!opts.signal) throw new Error("History requires cancellation");
      const signal = opts.signal;
      return new Promise((resolve) => { pages.push({ signal, resolve }); });
    });
    const view = renderHook<ReturnType<typeof useTimeline>, { id: number | null }>(
      ({ id }) => useTimeline(id, showError), { initialProps: { id: 1 }, wrapper },
    );
    await waitFor(() => expect(view.result.current.items).toHaveLength(3));
    await waitFor(() => expect(ctx.getQueryClient().getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());
    const compact = () => {
      pushEvent({ role: "compact_done", agent_id: 1 });
      pushEvent({ role: "timeline_snapshot", agent_id: 1, msg_count: 3, items: postWindow() });
    };
    compact();
    await waitFor(() => expect(pages).toHaveLength(1));
    const visibility = ctx.spyDocumentVisibility();
    try {
      if (cause === "switch") {
        view.rerender({ id: 2 });
        view.rerender({ id: 1 });
        await waitFor(() => expect(view.result.current.items).toHaveLength(3));
        compact();

        await waitFor(() => expect(pages).toHaveLength(2));
      } else if (cause === "hidden") {
        act(() => {
          visibility.mockReturnValue("hidden");
          ctx.dispatchVisibilityChange();
        });
      } else if (cause === "clear") {
        view.rerender({ id: null });
      } else {
        view.unmount();
      }
      expect(pages[0].signal.aborted).toBe(true);
      await act(async () => {
        pages[0].resolve(tlResp([snapshotItem({ item_id: "s1.old.1.0", payload: "obsolete retained page" })], true));
        await Promise.resolve();
      });
      expect(useTimelineStore.getState().items.some((item) => item.payload === "obsolete retained page")).toBe(false);
      expect(pages).toHaveLength(cause === "switch" ? 2 : 1);
      if (cause === "hidden") {
        act(() => {
          visibility.mockReturnValue("visible");
          ctx.dispatchVisibilityChange();
        });

        await waitFor(() => expect(pages).toHaveLength(2));
      }
      if (cause === "switch" || cause === "hidden") {
        expect(pages[1].signal.aborted).toBe(false);
        await act(async () => {
          pages[1].resolve(tlResp([snapshotItem({ item_id: "s1.new.1.0", payload: "current retained page" })], true));
          await Promise.resolve();
        });
        await waitFor(() => expect(pages).toHaveLength(3));
        await act(async () => {
          pages[2].resolve(tlResp([snapshotItem({ item_id: "s2.new.1.0", payload: "older retained page" })], false));
          await Promise.resolve();
        });
        expect(view.result.current.items.map((item) => item.payload)).toContain("older retained page");
      }
      expect(showError).not.toHaveBeenCalled();
    } finally {
      visibility.mockRestore();
    }
  });
});

describe("latest-message refresh lifecycle", () => {
  it("exposes the shared reconcile request as fetching until the new tail lands", async () => {
    ctx.getQueryClient().setQueryData(["timeline", 7], tlResp([snapshotItem({ item_id: "1.0", payload: "Cached reply" })]));
    let finish!: (snapshot: ReturnType<typeof composedSnapshot>) => void;
    vi.mocked(api.getConversationSnapshot).mockReset().mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const { result } = renderHook(() => useTimeline(7, vi.fn()), { wrapper });
    expect(result.current.isFetching).toBe(false);
    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(api.getConversationSnapshot).toHaveBeenCalledOnce());
    expect(result.current.isFetching).toBe(true);
    expect(result.current.items[0].payload).toBe("Cached reply");
    act(() => { finish(composedSnapshot(tlResp([snapshotItem({ item_id: "2.0", payload: "New reply" })]))); });
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(result.current.items.some((item) => item.payload === "New reply")).toBe(true);
  });

  it("settles lingering turn state and live clocks when agentStatus leaves running", async () => {
    ctx.getQueryClient().setQueryData(["timeline", 7], tlResp([]));
    const { result, rerender } = renderHook(
      ({ status }: { status?: string }) => useTimeline(7, vi.fn(), status),
      { wrapper, initialProps: { status: "running" } },
    );
    act(() => {
      pushEvent({ role: "code_start", agent_id: 7, item_id: "1.0" });
      pushEvent({ role: "code_delta", agent_id: 7, item_id: "1.0", content: "print(1)" });
      pushEvent({ role: "exec_start", agent_id: 7, item_id: "2.0" });
    });
    expect(result.current.turnActive).toBe(true);
    rerender({ status: "idle" });
    await waitFor(() => expect(result.current.turnActive).toBe(false));
    expect(result.current.items.map((it) => it.item_id)).toEqual(["1.0"]);
    expect(result.current.items[0].codeStartedAt).toBeUndefined();
    expect(result.current.items[0].codeElapsedMs).toEqual(expect.any(Number));
  });
});
}
