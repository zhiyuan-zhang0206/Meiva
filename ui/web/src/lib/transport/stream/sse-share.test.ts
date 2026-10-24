import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileSseTransport } from "./sse-share";
import type { SseChannel, SseListener } from "./sse-plan";
import { FakeNetwork, FakeTab, READY } from "./tests/fake-stream";
import { ScriptedAuth } from "./tests/scripted-auth";

const UNION_DEBOUNCE_MS_FOR_TEST = 250;

function tab(network: FakeNetwork, options: { merged?: SseChannel[]; solo?: boolean } = {}) {
  const fake = new FakeTab({
    network,
    capability: options.solo ? "legacy" : "shared",
    wallNow: () => Date.now(),
    secureContext: true,
  });
  const auth = new ScriptedAuth();
  const invalidations = { count: 0 };
  const transport = new ProfileSseTransport(
    fake.transport,
    {
      checkAuth: auth.checkAuth,
      sessionInvalid: () => {
        invalidations.count += 1;
      },
    },
    { base: "http://gateway.test", merged: new Set(options.merged ?? []), solo: options.solo ?? false },
  );
  const listen = (ch: "system" | "alerts" | "systemAll", activeId: number | null = null) => {
    const onFrame = vi.fn<(raw: string) => void>();
    const onState = vi.fn<(state: "open" | "reconnecting" | "closed") => void>();
    const onGap = vi.fn<(dropped: number) => void>();
    const unsubscribe = transport.subscribe(ch, { onFrame, onState, onGap } satisfies SseListener, activeId);
    return { onFrame, onState, onGap, unsubscribe };
  };
  return { transport, fake, auth, invalidations, sources: fake.sources, document: fake.page, listen };
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("profile SSE transport", () => {
  it("only the leader constructs sources and relays open state, heartbeat, and frames", async () => {
    const network = new FakeNetwork();
    const leader = tab(network);
    const follower = tab(network);
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
    const network = new FakeNetwork();
    const leader = tab(network);
    const follower = tab(network);
    leader.listen("system");
    const received = follower.listen("system");
    await settle();
    leader.sources[0].open();

    leader.transport.dispose();
    await settle();
    expect(leader.sources.every((source) => source.readyState === READY.CLOSED)).toBe(true);
    expect(follower.transport.isLeader()).toBe(true);
    expect(follower.sources).toHaveLength(2);
    expect(received.onState).toHaveBeenLastCalledWith("reconnecting");
    follower.sources[0].open();
    expect(received.onState).toHaveBeenLastCalledWith("open");
    follower.transport.dispose();
    await settle();
  });

  it("hide aborts a pending lock, closes a held lock, and show requests again", async () => {
    const network = new FakeNetwork();
    const leader = tab(network);
    const follower = tab(network);
    leader.listen("system");
    follower.listen("system");
    await settle();
    follower.document.show(false);
    leader.document.show(false);
    await settle();
    expect(leader.sources.every((source) => source.readyState === READY.CLOSED)).toBe(true);
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
    const network = new FakeNetwork();
    const leader = tab(network);
    const follower = tab(network);
    leader.listen("systemAll", 7);
    const detail = follower.listen("systemAll", 3);
    await settle();
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=3,7");
    leader.sources.at(-1)?.open();
    const firstDetail = leader.sources.at(-1);

    detail.unsubscribe();
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(firstDetail?.readyState).toBe(READY.CLOSED);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=7");
    expect(leader.sources.at(-1)?.readyState).toBe(READY.CONNECTING);

    follower.listen("systemAll", 3);
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=3,7");
    // meiva:note
    // Simulate a crashed tab: it cannot send leave or answer pings.
    follower.fake.channels[0].close();
    await vi.advanceTimersByTimeAsync(50_250);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=7");
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("mirrors errors without a follower auth probe or follower source", async () => {
    const network = new FakeNetwork();
    const leader = tab(network);
    const follower = tab(network);
    leader.listen("alerts");
    const received = follower.listen("alerts");
    await settle();
    leader.sources[1].error(READY.CONNECTING);
    expect(received.onState).toHaveBeenCalledWith("reconnecting");
    leader.sources[1].error(READY.CLOSED);
    await settle();
    expect(received.onState).toHaveBeenCalledWith("closed");
    expect(leader.auth.probes).toBe(1);
    expect(follower.auth.probes).toBe(0);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(leader.sources).toHaveLength(3);
    expect(follower.sources).toHaveLength(0);
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("does not replay a stale open after a restart changes the state", async () => {
    const leader = tab(new FakeNetwork());
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

  it("handles a non-abort Web Lock rejection without an unhandled promise", async () => {
    const network = new FakeNetwork();
    const failure = new Error("lock manager failed");
    network.locks.failure = failure;
    const failed = tab(network);
    failed.listen("system");
    await settle();
    expect(failed.transport.isLeader()).toBe(false);
    expect(failed.sources).toHaveLength(0);
    expect(failed.fake.errors).toEqual([{ message: "[sse-share] Web Lock failed", cause: failure }]);
    failed.transport.dispose();
  });

  it("clears leadership and closes partial sources when the lock callback throws", async () => {
    const failed = tab(new FakeNetwork());
    failed.fake.failNextSourceOnPath("/api/alerts/stream");
    failed.listen("system");
    await settle();
    expect(failed.transport.isLeader()).toBe(false);
    expect(failed.sources).toHaveLength(1);
    expect(failed.sources[0].readyState).toBe(READY.CLOSED);
    expect(failed.fake.errors).toEqual([
      { message: "[sse-share] Web Lock failed", cause: expect.any(Error) as unknown },
    ]);
    failed.transport.dispose();
  });

  it("carries every merged channel on one source, without the per-channel slots", async () => {
    const leader = tab(new FakeNetwork(), { merged: ["system", "systemAll", "alerts"] });
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
    const leader = tab(new FakeNetwork(), { merged: ["system", "systemAll", "alerts"] });
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
    const leader = tab(new FakeNetwork(), { merged: ["system", "systemAll", "alerts"] });
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
    const leader = tab(new FakeNetwork(), { merged: ["system", "systemAll", "alerts"] });
    const system = leader.listen("system");
    const detail = leader.listen("systemAll", 3);
    await settle();
    const source = leader.sources[0];
    source.open();
    source.frame('{"ch":"all","error":"event stream interrupted: ConnectionError"}');
    expect(detail.onState).toHaveBeenLastCalledWith("reconnecting");
    expect(system.onState).toHaveBeenLastCalledWith("open");
    expect(leader.sources[0].readyState).toBe(READY.OPEN);
    expect(leader.fake.warnings).toEqual([
      "[sse-share] channel systemAll degraded: event stream interrupted: ConnectionError",
    ]);
    leader.transport.dispose();
    await settle();
  });

  it("reports a shed count as a gap without inventing frames", async () => {
    const leader = tab(new FakeNetwork(), { merged: ["system", "systemAll", "alerts"] });
    const detail = leader.listen("systemAll", 3);
    await settle();
    leader.sources[0].frame('{"ch":"all","events":[],"dropped":4}');
    expect(detail.onGap).toHaveBeenCalledWith(4);
    expect(detail.onFrame).not.toHaveBeenCalled();
    leader.transport.dispose();
    await settle();
  });

  it("owns the merged source alone when the page has no lock manager", async () => {
    const solo = tab(new FakeNetwork(), { merged: ["system", "systemAll", "alerts"], solo: true });
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
    expect(solo.sources[0].readyState).toBe(READY.CLOSED);
    solo.document.show(true);
    await settle();
    expect(solo.sources).toHaveLength(2);
    expect(solo.sources[1].readyState).toBe(READY.CONNECTING);
    solo.transport.dispose();
    await settle();
  });
});
