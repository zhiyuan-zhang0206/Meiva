import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConnectionEvent } from "../../useEventStream";
import { useEventStream } from "../../useEventStream";
import type { EventStreamTestContext } from "./use-event-stream-context";

export function registerEventStreamProviderTests(ctx: EventStreamTestContext): void {
  const {
    READY,
    expectInstance,
    waitForInstance,
    withProvider,
    useAuth,
  } = ctx;

describe("EventStreamProvider onerror three-way readyState dispatch", () => {
  it("readyState=CLOSED → onConnectionEvent({type:'closed'})", async () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => expectInstance().error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });

    expect(onConn).toHaveBeenCalledWith({ type: "closed" });
  });

  it("readyState=CONNECTING → onConnectionEvent({type:'reconnecting'})", async () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => expectInstance().error(READY.CONNECTING));

    expect(onConn).toHaveBeenCalledWith({ type: "reconnecting" });
  });

  it("readyState=OPEN → no notification (transient fault absorbed by the browser)", async () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => {
      expectInstance().open();
      onConn.mockClear();
      expectInstance().error(READY.OPEN);
    });

    expect(onConn).not.toHaveBeenCalled();
  });

  it("unknown readyState → throws (fail-fast, no case _: catch-all)", async () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    expect(() => {
      act(() => expectInstance().error(99));
    }).toThrow(/unknown EventSource readyState: 99/);
  });
});

describe("EventStreamProvider auth-gated connection", () => {
  it("unauthenticated → no EventSource is constructed", async () => {
    ctx.auth.always({ authenticated: false });

    const { result } = renderHook(() => useAuth().status, { wrapper: withProvider() });

    await waitFor(() => expect(result.current).toBe("unauthenticated"));
    expect(ctx.getLastInstance()).toBeNull();
  });

  it("CLOSED with an invalid session → notifySessionInvalid, NO reopen", async () => {
    try {
      const { result } = renderHook(
        () => {
          useEventStream(() => undefined, () => undefined);
          return useAuth().status;
        },
        { wrapper: withProvider() },
      );
      await waitForInstance();
      const first = expectInstance();
      await waitFor(() => expect(result.current).toBe("authenticated"));

      vi.useFakeTimers();
      ctx.auth.always({ authenticated: false });
      act(() => first.error(READY.CLOSED));
      await act(async () => {
        await Promise.resolve();
      });

      vi.useRealTimers();
      await waitFor(() => expect(result.current).toBe("unauthenticated"));
      vi.useFakeTimers();
      act(() => {
        vi.advanceTimersByTime(60_000);
      });
      expect(expectInstance()).toBe(first);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a late invalid CLOSED probe cannot invalidate a replacement provider", async () => {
    ctx.auth.once({ authenticated: true });
    const staleProbe = ctx.auth.onceDeferred();
    ctx.auth.once({ authenticated: true });

    const firstMount = renderHook(
      () => {
        useEventStream(() => undefined, () => undefined);
        return useAuth().status;
      },
      { wrapper: withProvider() },
    );
    await waitFor(() => expect(firstMount.result.current).toBe("authenticated"));
    await waitForInstance();
    act(() => expectInstance().error(READY.CLOSED));
    firstMount.unmount();

    ctx.resetInstances();
    const replacement = renderHook(
      () => {
        useEventStream(() => undefined, () => undefined);
        return useAuth().status;
      },
      { wrapper: withProvider() },
    );
    await waitFor(() => expect(replacement.result.current).toBe("authenticated"));
    await waitForInstance();
    const replacementSource = expectInstance();

    await act(async () => {
      staleProbe.release({ authenticated: false });
      await Promise.resolve();
    });

    expect(replacement.result.current).toBe("authenticated");
    expect(expectInstance()).toBe(replacementSource);
    expect(replacementSource.readyState).not.toBe(READY.CLOSED);
  });
});

describe("EventStreamProvider connect-failure backoff retry", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("three malformed frames close the old source before the backoff retry", async () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    const first = expectInstance();

    vi.useFakeTimers();
    act(() => {
      first.open();
      first.frame("{");
      first.frame("{");
      first.frame("{");
    });
    expect(first.readyState).toBe(READY.CLOSED);
    act(() => { vi.advanceTimersByTime(999); });
    expect(expectInstance()).toBe(first);
    act(() => { vi.advanceTimersByTime(1); });
    expect(expectInstance()).not.toBe(first);
  });

  it("dead-end CLOSED error schedules a backoff reopen (a fresh EventSource after the delay)", async () => {

    const onConn = vi.fn<(ev: ConnectionEvent) => void>();
    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    await waitForInstance();
    vi.useFakeTimers();
    const first = expectInstance();

    act(() => first.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    expect(onConn).toHaveBeenCalledWith({ type: "closed" });

    expect(expectInstance()).toBe(first);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(expectInstance()).not.toBe(first);
  });

  it("CONNECTING error does NOT schedule a manual reopen (browser already auto-retries)", async () => {

    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    vi.useFakeTimers();
    const first = expectInstance();

    act(() => first.error(READY.CONNECTING));
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(expectInstance()).toBe(first);
  });

  it("backoff grows across consecutive failures (2nd retry waits longer than the 1st)", async () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    vi.useFakeTimers();
    const first = expectInstance();

    act(() => first.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    const second = expectInstance();
    expect(second).not.toBe(first);

    act(() => second.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(expectInstance()).toBe(second);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(expectInstance()).not.toBe(second);
  });

  it("a successful open resets the backoff (next failure retries at the base delay again)", async () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    vi.useFakeTimers();
    const first = expectInstance();

    act(() => first.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    const second = expectInstance();

    act(() => second.open());

    act(() => second.error(READY.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(expectInstance()).not.toBe(second);
  });
});

describe("EventStreamProvider cleanup + thread switch", () => {
  it("unmount → old EventSource closed, remount opens a new connection", async () => {

    const { unmount } = renderHook(() => useEventStream(vi.fn(), vi.fn()), {
      wrapper: withProvider(),
    });
    await waitForInstance();
    const first = expectInstance();
    expect(first.url).toContain("/api/system");
    expect(first.readyState).toBe(READY.CONNECTING);

    unmount();
    expect(first.readyState).toBe(READY.CLOSED);

    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    expect(expectInstance().url).toContain("/api/system");
  });

  it("multiple subscribers share one EventSource (core: no reconnect)", async () => {

    const onSystem1 = vi.fn();
    const onSystem2 = vi.fn();
    const wrapper = withProvider();

    function useTwoSubscribers() {
      useEventStream(onSystem1, vi.fn());
      useEventStream(onSystem2, vi.fn());
    }
    renderHook(() => useTwoSubscribers(), { wrapper });
    await waitForInstance();

    const inst = expectInstance();
    act(() => {
      inst.open();
      inst.frame(JSON.stringify({ role: "llm_done", agent_id: 42 }));
    });

    expect(onSystem1).toHaveBeenCalledWith({ role: "llm_done", agent_id: 42 });
    expect(onSystem2).toHaveBeenCalledWith({ role: "llm_done", agent_id: 42 });
  });
});

describe("EventStreamProvider heartbeat frame", () => {
  it("heartbeat frame is liveness-only — NOT fanned out as a business event", async () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    await waitForInstance();
    act(() => {
      expectInstance().open();
      expectInstance().frame(JSON.stringify({ role: "heartbeat" }));
    });

    expect(onSystem).not.toHaveBeenCalled();
    expect(onConn).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: "parse-failed" }),
    );
  });

  it("a real business event after a heartbeat still flows through", async () => {
    const onSystem = vi.fn();

    renderHook(() => useEventStream(onSystem, vi.fn()), { wrapper: withProvider() });
    await waitForInstance();
    act(() => {
      expectInstance().open();
      expectInstance().frame(JSON.stringify({ role: "heartbeat" }));
      expectInstance().frame(JSON.stringify({ role: "llm_done", agent_id: 7 }));
    });

    expect(onSystem).toHaveBeenCalledTimes(1);
    expect(onSystem).toHaveBeenCalledWith({ role: "llm_done", agent_id: 7 });
  });
});

}
