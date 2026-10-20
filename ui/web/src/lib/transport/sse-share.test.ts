import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../telemetry/telemetry", () => ({ track: vi.fn() }));

import { track } from "../telemetry/telemetry";
import { ProfileSseTransport, reportSseTransportMode, sharedSseSupported, type SseChannel, type SseListener } from "./sse-share";

class FakeChannel {
  static channels = new Set<FakeChannel>();
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;

  constructor() { FakeChannel.channels.add(this); }
  postMessage(data: unknown): void {
    for (const peer of FakeChannel.channels) {
      if (peer !== this) peer.onmessage?.({ data } as MessageEvent<unknown>);
    }
  }
  close(): void { FakeChannel.channels.delete(this); }
}

class FakeDocument extends EventTarget {
  visibilityState: DocumentVisibilityState = "visible";
  show(visible: boolean): void {
    this.visibilityState = visible ? "visible" : "hidden";
    this.dispatchEvent(new Event("visibilitychange"));
  }
}

interface QueuedLock {
  callback: (lock: Lock) => Promise<void>;
  signal: AbortSignal;
  resolve: () => void;
  reject: (error: unknown) => void;
}

class FakeLocks {
  private held = false;
  private queue: QueuedLock[] = [];
  failure: Error | null = null;

  request(
    name: string,
    options: LockOptions,
    callback: (lock: Lock) => Promise<void>,
  ): Promise<void> {
    if (this.failure !== null) return Promise.reject(this.failure);
    expect(name).toBe("meiva-ui-sse");
    expect(options.mode).toBe("exclusive");
    const signal = options.signal;
    if (!signal) throw new Error("missing lock abort signal");
    return new Promise<void>((resolve, reject) => {
      const item = { callback, signal, resolve, reject };
      this.queue.push(item);
      signal.addEventListener("abort", () => {
        if (!this.queue.includes(item)) return;
        this.queue = this.queue.filter((queued) => queued !== item);
        reject(new DOMException("aborted", "AbortError"));
      });
      this.pump();
    });
  }

  private pump(): void {
    if (this.held) return;
    const item = this.queue.shift();
    if (!item) return;
    this.held = true;
    void item.callback({ name: "meiva-ui-sse", mode: "exclusive" })
      .then(item.resolve, item.reject)
      .finally(() => {
        this.held = false;
        this.pump();
      });
  }
}

class FakeSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static failOnPath: string | null = null;
  readyState = FakeSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    if (FakeSource.failOnPath !== null && url.endsWith(FakeSource.failOnPath)) {
      FakeSource.failOnPath = null;
      throw new Error("source construction failed");
    }
  }
  close(): void { this.readyState = FakeSource.CLOSED; }
  open(): void {
    this.readyState = FakeSource.OPEN;
    this.onopen?.();
  }
  frame(data: string): void { this.onmessage?.({ data } as MessageEvent<string>); }
  error(state: number): void {
    this.readyState = state;
    this.onerror?.();
  }
}

function tab(locks: FakeLocks, options: { merged?: SseChannel[]; solo?: boolean } = {}) {
  const document = new FakeDocument();
  const channel = new FakeChannel();
  const sources: FakeSource[] = [];
  const checkAuth = vi.fn(() => Promise.resolve({ authenticated: true }));
  const sessionInvalid = vi.fn();
  const solo = options.solo ?? false;
  const transport = new ProfileSseTransport(
    {
      channel: solo ? null : (channel as unknown as BroadcastChannel),
      locks: solo ? null : (locks as unknown as LockManager),
      document: document as unknown as Document,
      source: (url: string) => {
        const source = new FakeSource(url);
        sources.push(source);
        return source as unknown as EventSource;
      },
      checkAuth,
      sessionInvalid,
      now: Date.now,
    },
    new Set(options.merged ?? []),
    solo,
  );
  const listen = (ch: "system" | "alerts" | "systemAll", activeId: number | null = null) => {
    const onFrame = vi.fn<(raw: string) => void>();
    const onState = vi.fn<(state: "open" | "reconnecting" | "closed") => void>();
    const onGap = vi.fn<(dropped: number) => void>();
    const unsubscribe = transport.subscribe(ch, { onFrame, onState, onGap } satisfies SseListener, activeId);
    return { onFrame, onState, onGap, unsubscribe };
  };
  return { transport, document, channel, sources, checkAuth, sessionInvalid, listen };
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", FakeSource);
  FakeChannel.channels.clear();
  FakeSource.failOnPath = null;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeChannel.channels.clear();
});

describe("profile SSE transport", () => {
  it("only the leader constructs sources and relays open state, heartbeat, and frames", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("system");
    const received = follower.listen("system");
    await settle();

    expect(leader.transport.isLeader()).toBe(true);
    expect(follower.transport.isLeader()).toBe(false);
    expect(leader.sources.map((source) => new URL(source.url).pathname)).toEqual([
      "/api/system", "/api/alerts/stream",
    ]);
    expect(follower.sources).toHaveLength(0);
    leader.sources[0].open();
    leader.sources[0].frame('{"role":"heartbeat"}');
    leader.sources[0].frame('{"role":"agent_started"}');
    expect(received.onState).toHaveBeenCalledWith("open");
    expect(received.onFrame.mock.calls.map(([raw]) => raw)).toEqual([
      '{"role":"heartbeat"}', '{"role":"agent_started"}',
    ]);
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("promotes a waiting visible tab after leader close", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("system");
    const received = follower.listen("system");
    await settle();
    leader.sources[0].open();

    leader.transport.dispose();
    await settle();
    expect(leader.sources.every((source) => source.readyState === FakeSource.CLOSED)).toBe(true);
    expect(follower.transport.isLeader()).toBe(true);
    expect(follower.sources).toHaveLength(2);
    expect(received.onState).toHaveBeenLastCalledWith("reconnecting");
    follower.sources[0].open();
    expect(received.onState).toHaveBeenLastCalledWith("open");
    follower.transport.dispose();
    await settle();
  });

  it("hide aborts a pending lock, closes a held lock, and show requests again", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("system");
    follower.listen("system");
    await settle();
    follower.document.show(false);
    leader.document.show(false);
    await settle();
    expect(leader.sources.every((source) => source.readyState === FakeSource.CLOSED)).toBe(true);
    expect(follower.sources).toHaveLength(0);

    follower.document.show(true);
    await settle();
    expect(follower.transport.isLeader()).toBe(true);
    expect(follower.sources).toHaveLength(2);
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("opens a sorted detail union and removes changed or stale interests", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("systemAll", 7);
    const detail = follower.listen("systemAll", 3);
    await settle();
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=3,7");
    leader.sources.at(-1)?.open();
    const firstDetail = leader.sources.at(-1);

    detail.unsubscribe();
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(firstDetail?.readyState).toBe(FakeSource.CLOSED);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=7");
    expect(leader.sources.at(-1)?.readyState).toBe(FakeSource.CONNECTING);

    follower.listen("systemAll", 3);
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=3,7");
    // Simulate a crashed tab: it cannot send leave or answer pings.
    follower.channel.close();
    await vi.advanceTimersByTimeAsync(50_250);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=7");
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("mirrors errors without a follower auth probe or follower source", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("alerts");
    const received = follower.listen("alerts");
    await settle();
    leader.sources[1].error(FakeSource.CONNECTING);
    expect(received.onState).toHaveBeenCalledWith("reconnecting");
    leader.sources[1].error(FakeSource.CLOSED);
    await settle();
    expect(received.onState).toHaveBeenCalledWith("closed");
    expect(leader.checkAuth).toHaveBeenCalledTimes(1);
    expect(follower.checkAuth).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(leader.sources).toHaveLength(3);
    expect(follower.sources).toHaveLength(0);
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("does not replay a stale open after a restart changes the state", async () => {
    const leader = tab(new FakeLocks());
    leader.listen("system");
    await settle();
    leader.sources[0].open();
    const late = leader.listen("system");
    leader.transport.restart("system");
    leader.transport.restart("system");
    await settle();

    expect(late.onState.mock.calls.map(([state]) => state)).toEqual(["reconnecting"]);
    expect(leader.sources.filter((source) => source.url.endsWith("/api/system"))).toHaveLength(2);
    leader.transport.dispose();
    await settle();
  });

  it("detects missing platform APIs so Providers retain their local fallback", () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    expect(sharedSseSupported()).toBe(false);
    vi.unstubAllGlobals();
    const original = navigator.locks;
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    expect(sharedSseSupported()).toBe(false);
    Object.defineProperty(navigator, "locks", { configurable: true, value: original });
  });

  it("handles a non-abort Web Lock rejection without an unhandled promise", async () => {
    const locks = new FakeLocks();
    locks.failure = new Error("lock manager failed");
    const failed = tab(locks);
    const report = vi.spyOn(console, "error").mockImplementation(() => undefined);
    failed.listen("system");
    await settle();
    expect(failed.transport.isLeader()).toBe(false);
    expect(failed.sources).toHaveLength(0);
    expect(report).toHaveBeenCalledWith("[sse-share] Web Lock failed", locks.failure);
    failed.transport.dispose();
    report.mockRestore();
  });

  it("clears leadership and closes partial sources when the lock callback throws", async () => {
    const failed = tab(new FakeLocks());
    FakeSource.failOnPath = "/api/alerts/stream";
    const report = vi.spyOn(console, "error").mockImplementation(() => undefined);
    failed.listen("system");
    await settle();
    expect(failed.transport.isLeader()).toBe(false);
    expect(failed.sources).toHaveLength(1);
    expect(failed.sources[0].readyState).toBe(FakeSource.CLOSED);
    expect(report).toHaveBeenCalledWith("[sse-share] Web Lock failed", expect.any(Error));
    failed.transport.dispose();
    report.mockRestore();
  });

  it("carries every merged channel on one source, without the per-channel slots", async () => {
    const leader = tab(new FakeLocks(), { merged: ["system", "systemAll", "alerts"] });
    leader.listen("system");
    await settle();
    expect(leader.sources).toHaveLength(1);
    const url = new URL(leader.sources[0].url);
    expect(url.pathname).toBe("/api/stream");
    expect(url.searchParams.get("channels")).toBe("global,all,alerts");
    expect(url.searchParams.get("agents")).toBe("0");
    leader.transport.dispose();
    await settle();
  });

  it("reopens the merged source with the union when the selection changes", async () => {
    const leader = tab(new FakeLocks(), { merged: ["system", "systemAll", "alerts"] });
    leader.listen("system");
    leader.listen("systemAll", 7);
    await settle();
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    const urls = leader.sources.map((source) => new URL(source.url).searchParams.get("agents"));
    expect(urls).toEqual(["0", "7"]);
    leader.transport.dispose();
    await settle();
  });

  it("hands each wire channel to its subscribers in the shape they already parse", async () => {
    const leader = tab(new FakeLocks(), { merged: ["system", "systemAll", "alerts"] });
    const system = leader.listen("system");
    const detail = leader.listen("systemAll", 3);
    const alerts = leader.listen("alerts");
    await settle();
    const source = leader.sources[0];
    source.open();
    source.frame('{"ch":"global","events":[{"role":"label_updated","agent_id":3}]}');
    source.frame('{"ch":"all","events":[{"role":"chat_delta"},{"role":"idle"}]}');
    source.frame('{"ch":"alerts","events":[{"id":1,"status":"unresolved"}]}');

    expect(system.onFrame.mock.calls.map(([raw]) => raw)).toEqual([
      '{"role":"label_updated","agent_id":3}',
    ]);
    expect(detail.onFrame.mock.calls.map(([raw]) => raw)).toEqual([
      '[{"role":"chat_delta"},{"role":"idle"}]',
    ]);
    expect(alerts.onFrame.mock.calls.map(([raw]) => raw)).toEqual([
      '{"id":1,"status":"unresolved"}',
    ]);
    leader.transport.dispose();
    await settle();
  });

  it("degrades only the channel an error frame names", async () => {
    const leader = tab(new FakeLocks(), { merged: ["system", "systemAll", "alerts"] });
    const system = leader.listen("system");
    const detail = leader.listen("systemAll", 3);
    await settle();
    const source = leader.sources[0];
    source.open();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    source.frame('{"ch":"all","error":"event stream interrupted: ConnectionError"}');
    expect(detail.onState).toHaveBeenLastCalledWith("reconnecting");
    expect(system.onState).toHaveBeenLastCalledWith("open");
    expect(leader.sources[0].readyState).toBe(FakeSource.OPEN);
    warn.mockRestore();
    leader.transport.dispose();
    await settle();
  });

  it("reports a shed count as a gap without inventing frames", async () => {
    const leader = tab(new FakeLocks(), { merged: ["system", "systemAll", "alerts"] });
    const detail = leader.listen("systemAll", 3);
    await settle();
    leader.sources[0].frame('{"ch":"all","events":[],"dropped":4}');
    expect(detail.onGap).toHaveBeenCalledWith(4);
    expect(detail.onFrame).not.toHaveBeenCalled();
    leader.transport.dispose();
    await settle();
  });

  it("owns the merged source alone when the page has no lock manager", async () => {
    const solo = tab(new FakeLocks(), { merged: ["system", "systemAll", "alerts"], solo: true });
    const received = solo.listen("system");
    await settle();
    expect(solo.transport.isLeader()).toBe(true);
    expect(solo.sources).toHaveLength(1);
    solo.sources[0].open();
    solo.sources[0].frame('{"ch":"global","events":[{"role":"agent_spawned"}]}');
    expect(received.onState).toHaveBeenCalledWith("open");
    expect(received.onFrame.mock.calls.map(([raw]) => raw)).toEqual(['{"role":"agent_spawned"}']);
    solo.document.show(false);
    await settle();
    expect(solo.sources[0].readyState).toBe(FakeSource.CLOSED);
    solo.document.show(true);
    await settle();
    expect(solo.sources).toHaveLength(2);
    expect(solo.sources[1].readyState).toBe(FakeSource.CONNECTING);
    solo.transport.dispose();
    await settle();
  });

  it("reports the fallback mode and capability flags once per tab session", () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    const original = navigator.locks;
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    reportSseTransportMode();
    reportSseTransportMode();
    expect(track).toHaveBeenCalledTimes(1);
    expect(track).toHaveBeenCalledWith("sse-transport", {
      key: "fallback", value: expect.stringContaining("locks=0,bc=0") as string,
    });
    Object.defineProperty(navigator, "locks", { configurable: true, value: original });
  });
});

const UNION_DEBOUNCE_MS_FOR_TEST = 250;
