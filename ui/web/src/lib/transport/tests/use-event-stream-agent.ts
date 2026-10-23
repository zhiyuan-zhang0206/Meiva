import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SystemEvent } from "../../contracts/types";
import type { ConnectionEvent } from "../useEventStream";
import { useAgentEventStream } from "../useEventStream";
import type { EventStreamTestContext } from "./use-event-stream-context";

export function registerAgentEventStreamTests(ctx: EventStreamTestContext): void {
  const {
    MockEventSource,
    expectInstance,
    waitForInstance,
    useAuth,
  } = ctx;

describe("AgentEventStreamProvider selected detail stream", () => {
  beforeEach(() => { ctx.setStoreState({ activeId: 42 }); });
  const withAgentProvider = ctx.withAgentProvider;

  it("activeId filters the /api/system/all URL", async () => {
    act(() => ctx.setStoreState({ activeId: 7 }));
    void renderHook(() => useAgentEventStream(vi.fn(), vi.fn()), {
      wrapper: withAgentProvider(),
    });
    await waitForInstance();
    expect(expectInstance().url).toBe("/api/system/all?agents=7");
  });

  it("no selected agent means no detail connection", async () => {
    ctx.setStoreState({ activeId: null });
    const { result } = renderHook(() => {
      useAgentEventStream(vi.fn(), vi.fn());
      return useAuth().status;
    }, { wrapper: withAgentProvider() });
    await waitFor(() => expect(result.current).toBe("authenticated"));
    expect(ctx.getLastInstance()).toBeNull();
  });

  it("clearing selection closes the detail connection and discards late frames", async () => {
    ctx.setStoreState({ activeId: 1 });
    const onSystem = vi.fn();
    renderHook(() => useAgentEventStream(onSystem, vi.fn()), { wrapper: withAgentProvider() });
    await waitForInstance();
    const old = expectInstance();
    act(() => ctx.setStoreState({ activeId: null }));
    expect(old.readyState).toBe(MockEventSource.CLOSED);
    act(() => old.fireMessage(JSON.stringify({ role: "chat_delta", agent_id: 1, item_id: "1.0", content: "late" })));
    expect(onSystem).not.toHaveBeenCalled();
  });

  it("visiting more agents never increases live detail connection count", async () => {
    ctx.setStoreState({ activeId: 1 });
    const { unmount } = renderHook(() => useAgentEventStream(vi.fn(), vi.fn()), { wrapper: withAgentProvider() });
    await waitForInstance();
    for (let id = 2; id <= 100; id += 1) act(() => ctx.setStoreState({ activeId: id }));
    expect(ctx.getInstances().filter((stream) => stream.readyState !== MockEventSource.CLOSED)).toHaveLength(1);
    expect(expectInstance().url).toBe("/api/system/all?agents=100");
    unmount();
    expect(ctx.getInstances().every((stream) => stream.readyState === MockEventSource.CLOSED)).toBe(true);
  });

  it("agent switch → closes and reopens with the new filter", async () => {
    act(() => ctx.setStoreState({ activeId: 1 }));
    void renderHook(() => useAgentEventStream(vi.fn(), vi.fn()), {
      wrapper: withAgentProvider(),
    });
    await waitForInstance();
    const first = expectInstance();
    expect(first.url).toBe("/api/system/all?agents=1");

    act(() => ctx.setStoreState({ activeId: 2 }));
    await waitFor(() => expect(expectInstance()).not.toBe(first));
    expect(first.readyState).toBe(MockEventSource.CLOSED);
    expect(expectInstance().url).toBe("/api/system/all?agents=2");
  });

  it("hidden page closes SSE without polling, then reopens when visible", async () => {
    act(() => ctx.setStoreState({ activeId: 7 }));
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();
    void renderHook(() => useAgentEventStream(vi.fn(), onConn), {
      wrapper: withAgentProvider(),
    });
    await waitForInstance();
    const first = expectInstance();

    try {
      vi.useFakeTimers();
      act(() => {
        ctx.setDocumentVisibility("hidden");
      });

      expect(first.readyState).toBe(MockEventSource.CLOSED);
      onConn.mockClear();
      act(() => {
        vi.advanceTimersByTime(7_000);
      });
      expect(onConn).not.toHaveBeenCalled();

      onConn.mockClear();
      act(() => {
        ctx.setDocumentVisibility("visible");
      });

      expect(expectInstance()).not.toBe(first);
      expect(expectInstance().url).toBe("/api/system/all?agents=7");
      act(() => {
        vi.advanceTimersByTime(7_000);
      });
      expect(onConn).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("calling useAgentEventStream outside its Provider → throws (fail-fast)", () => {
    const errSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined );
    expect(() => renderHook(() => useAgentEventStream(vi.fn(), vi.fn()))).toThrow(
      /useAgentEventStream/,
    );
    errSpy.mockRestore();
  });

  it("batched array frame → fans out each element as individual SystemEvent to subscribers", async () => {
    act(() => ctx.setStoreState({ activeId: 42 }));
    const onSystem = vi.fn();
    void renderHook(() => useAgentEventStream(onSystem, vi.fn()), {
      wrapper: withAgentProvider(),
    });
    await waitForInstance();
    act(() => {
      expectInstance().fireOpen();

      expectInstance().fireMessage(
        JSON.stringify([
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "hello" },
          { role: "code_delta", agent_id: 7, item_id: "1.0", content: "print" },
          { role: "agent_updated", agent_id: 42, snapshot: { id: 42 } },
        ]),
      );
    });

    expect(onSystem).toHaveBeenCalledTimes(3);
    expect(onSystem).toHaveBeenCalledWith({
      role: "chat_delta",
      agent_id: 7,
      item_id: "1.0",
      content: "hello",
    });
    expect(onSystem).toHaveBeenCalledWith({
      role: "code_delta",
      agent_id: 7,
      item_id: "1.0",
      content: "print",
    });
    expect(onSystem).toHaveBeenCalledWith({
      role: "agent_updated",
      agent_id: 42,
      snapshot: { id: 42 },
    });
  });

  it("a throwing subscriber neither blocks other subscribers nor counts as parse-failed", async () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();
    const onSystemBoom = vi.fn(() => {
      throw new Error("subscriber bug");
    });

    function useDualSubscriber() {
      useAgentEventStream(onSystem, onConn);
      useAgentEventStream(onSystemBoom, vi.fn());
    }
    void renderHook(() => useDualSubscriber(), {
      wrapper: withAgentProvider(),
    });
    await waitForInstance();
    const errSpy = ctx.spyConsoleError();
    act(() => {
      expectInstance().fireMessage(
        JSON.stringify({ role: "chat_delta", agent_id: 7, item_id: "1.0", content: "hi" }),
      );
    });
    errSpy.mockRestore();

    expect(onSystem).toHaveBeenCalledTimes(1);

    expect(onSystemBoom).toHaveBeenCalledTimes(1);
    expect(onConn).not.toHaveBeenCalled();
  });

  it("non-object elements in a batch frame are dropped, not fanned out", async () => {
    const onSystem = vi.fn();
    void renderHook(() => useAgentEventStream(onSystem, vi.fn()), {
      wrapper: withAgentProvider(),
    });
    await waitForInstance();
    act(() => {
      expectInstance().fireMessage(
        JSON.stringify([
          "corrupt-string-element",
          42,
          { role: "agent_updated", agent_id: 42, snapshot: { id: 42 } },
        ]),
      );
    });
    expect(onSystem).toHaveBeenCalledTimes(1);
    expect(onSystem).toHaveBeenCalledWith({
      role: "agent_updated",
      agent_id: 42,
      snapshot: { id: 42 },
    });
  });

  it("batch with heartbeat inside → heartbeat skipped, rest fanned out", async () => {
    const onSystem = vi.fn();
    void renderHook(() => useAgentEventStream(onSystem, vi.fn()), {
      wrapper: withAgentProvider(),
    });
    await waitForInstance();
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(
        JSON.stringify([
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "a" },
          { role: "heartbeat" },
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "b" },
        ]),
      );
    });

    expect(onSystem).toHaveBeenCalledTimes(2);
    expect(onSystem).toHaveBeenCalledWith({
      role: "chat_delta",
      agent_id: 7,
      item_id: "1.0",
      content: "a",
    });
    expect(onSystem).toHaveBeenCalledWith({
      role: "chat_delta",
      agent_id: 7,
      item_id: "1.0",
      content: "b",
    });
  });

  it("batch subscriber gets the whole frame in ONE call; its per-event callback is not invoked", async () => {
    const onSystem = vi.fn();
    const onBatch = vi.fn<(events: SystemEvent[]) => void>();
    void renderHook(
      () => useAgentEventStream(onSystem, vi.fn(), onBatch),
      { wrapper: withAgentProvider() },
    );
    await waitForInstance();
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(
        JSON.stringify([
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "a" },
          { role: "code_delta", agent_id: 7, item_id: "2.0", content: "b" },
        ]),
      );
    });

    expect(onBatch).toHaveBeenCalledTimes(1);
    expect(onBatch).toHaveBeenCalledWith([
      { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "a" },
      { role: "code_delta", agent_id: 7, item_id: "2.0", content: "b" },
    ]);

    expect(onSystem).not.toHaveBeenCalled();
  });

  it("a throwing batch subscriber never starves per-event subscribers of the same frame", async () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();
    const onBatchBoom = vi.fn(() => {
      throw new Error("batch subscriber bug");
    });
    function useDualSubscriber() {
      useAgentEventStream(onSystem, onConn);
      useAgentEventStream(vi.fn(), vi.fn(), onBatchBoom);
    }
    void renderHook(() => useDualSubscriber(), {
      wrapper: withAgentProvider(),
    });
    await waitForInstance();
    const errSpy = ctx.spyConsoleError();
    act(() => {
      expectInstance().fireMessage(
        JSON.stringify([
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "a" },
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "b" },
        ]),
      );
    });
    errSpy.mockRestore();

    expect(onSystem).toHaveBeenCalledTimes(2);

    expect(onBatchBoom).toHaveBeenCalledTimes(1);
    expect(onConn).not.toHaveBeenCalled();
  });

  it("single-event frame → batch subscriber receives a one-element array", async () => {
    const onBatch = vi.fn<(events: SystemEvent[]) => void>();
    void renderHook(
      () => useAgentEventStream(vi.fn(), vi.fn(), onBatch),
      { wrapper: withAgentProvider() },
    );
    await waitForInstance();
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(
        JSON.stringify({ role: "llm_done", agent_id: 7 }),
      );
    });
    expect(onBatch).toHaveBeenCalledTimes(1);
    expect(onBatch).toHaveBeenCalledWith([{ role: "llm_done", agent_id: 7 }]);
  });
});
}
