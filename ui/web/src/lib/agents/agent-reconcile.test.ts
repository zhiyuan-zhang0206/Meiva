// The composed conversation reconcile (agent-reconcile.ts): one read per
// re-attach, ordered so a composed write can never land older data after
// newer. Drives the coordinator factory directly — the hook wiring and the
// per-reader integration live in use-timeline.test.ts /
// use-detail-reconnect.test.ts.

import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { realNetwork, startGateway, type GatewayReply, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig, httpRig, noUpload, type HttpRig } from "../transport/tests/api/api-provider";
import { UncancellableFetch } from "../transport/tests/api/api-provider";
import { systemKeySource } from "../transport/system-transport";
import { createAgentReconcile } from "./agent-reconcile";
import type { ConversationSnapshotResponse, PendingInbound } from "../contracts/types";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function snapshot(tag: string): ConversationSnapshotResponse {
  return {
    timeline: { items: [], msg_count: 0, has_more: false },
    token_usage: { input_tokens: tag === "new" ? 2 : 1, output_tokens: 0, reasoning_tokens: 0,
      max_input_tokens: 100_000, soft_compact_tokens: 70_000, hard_compact_tokens: 90_000 },
    pending: [{ id: tag === "new" ? 2 : 1, content: tag, source: "user", sender: "user",
      images: null, created_at: "2026-09-18T00:00:00Z" }],
  };
}

function pendingRow(id: number): PendingInbound {
  return {
    id,
    content: String(id),
    source: "user",
    sender: "user",
    images: null,
    created_at: "2026-09-18T00:00:00Z",
  };
}

interface SnapshotRead {
  agentId: number;
  signal: AbortSignal | undefined;
}

type SnapshotAnswer = GatewayReply | Promise<GatewayReply>;
let gateway: TestGateway;
let rig: HttpRig;

let client: QueryClient;
let reconcile: ReturnType<typeof createAgentReconcile>;
let reads: SnapshotRead[];
let queuedAnswers: SnapshotAnswer[];
let defaultAnswer: SnapshotAnswer;

function resolves(value: ConversationSnapshotResponse): GatewayReply {
  return { kind: "json", body: value };
}

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  reads = [];
  queuedAnswers = [];
  defaultAnswer = { kind: "text", status: 500, body: "this test scripted no snapshot answer" };
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  gateway = await startGateway(realNetwork.createServer, (request) => {
    const match = /^\/api\/agents\/(\d+)\/conversation-snapshot$/.exec(request.path);
    if (match === null) return { kind: "text", status: 404, body: "unexpected route" };
    const signal = rig.sent.findLast((sent) => sent.url === `${gateway.origin}${request.path}`)?.init.signal;
    if (signal == null) throw new Error("the native request has no observed abort signal");
    reads.push({ agentId: Number(match[1]), signal });
    return queuedAnswers.shift() ?? defaultAnswer;
  });
  rig = gatewayRig(gateway);
  reconcile = createAgentReconcile(client, rig.api);
});

afterEach(async () => {
  reconcile.abort(42);
  client.clear();
  vi.useRealTimers();
  await gateway.close();
});

describe("composed conversation reconcile", () => {
  it("collapses a same-tick reader burst into one read and writes all three keys", async () => {
    defaultAnswer = resolves(snapshot("fresh"));
    const unsubscribes = [reconcile.subscribe(42), reconcile.subscribe(42), reconcile.subscribe(42)];

    reconcile.request(42);
    reconcile.request(42);
    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);

    await vi.waitFor(() => expect(reads).toHaveLength(1));
    expect(reads[0].agentId).toBe(42);
    expect(reads[0].signal).toBeInstanceOf(AbortSignal);
    await vi.waitFor(() => expect(client.getQueryData<PendingInbound[]>(["pending", 42])?.[0].content).toBe("fresh"));
    expect(client.getQueryData(["token-usage", 42])).toMatchObject({ input_tokens: 1 });
    expect(client.getQueryData(["timeline", 42])).toMatchObject({ msg_count: 0 });

    for (const unsubscribe of unsubscribes) unsubscribe();
  });

  it("joins an in-flight read: the composed read starts only after it settles", async () => {
    const inFlight = deferred<string>();
    void client.query({ queryKey: ["timeline", 42], queryFn: () => inFlight.promise });
    reconcile.subscribe(42);
    defaultAnswer = resolves(snapshot("fresh"));

    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(600); // several join polls, still fetching
    expect(reads).toHaveLength(0);

    inFlight.resolve("old");
    await vi.advanceTimersByTimeAsync(400);
    await vi.waitFor(() => expect(reads).toHaveLength(1));
  });

  it("a second gap during the composed read owes one trailing read", async () => {
    const first = deferred<GatewayReply>();
    queuedAnswers = [first.promise, resolves(snapshot("new"))];
    reconcile.subscribe(42);

    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(reads).toHaveLength(1));

    reconcile.request(42); // second open while the read is in flight
    first.resolve(resolves(snapshot("fresh")));
    await vi.advanceTimersByTimeAsync(400);

    await vi.waitFor(() => expect(reads).toHaveLength(2));
    await vi.waitFor(() => expect(client.getQueryData<PendingInbound[]>(["pending", 42])?.[0].content).toBe("new"));
  });

  it("drops a snapshot superseded by a newer write and re-reads once", async () => {
    const first = deferred<GatewayReply>();
    queuedAnswers = [first.promise, resolves(snapshot("new"))];
    reconcile.subscribe(42);
    client.setQueryData(["pending", 42], [pendingRow(1)]);

    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(reads).toHaveLength(1));
    // A newer snapshot lands while the composed read is in flight.
    client.setQueryData(["pending", 42], [pendingRow(1), pendingRow(2)]);
    first.resolve(resolves(snapshot("fresh")));
    await vi.advanceTimersByTimeAsync(400);

    // The stale composed payload never replaced the newer write; the trailing
    // read then delivered the newest server state.
    await vi.waitFor(() => expect(reads).toHaveLength(2));
    await vi.waitFor(() => expect(client.getQueryData<PendingInbound[]>(["pending", 42])?.[0].id).toBe(2));
    const pending = client.getQueryData<PendingInbound[]>(["pending", 42])!;
    expect(pending.map((row) => row.id)).toEqual([2]);
  });

  it("falls back to invalidating the three keys when the composed read fails", async () => {
    defaultAnswer = { kind: "text", status: 503, body: "gateway down" };
    const domains = ["timeline", "token-usage", "pending"];
    for (const domain of domains) client.setQueryData([domain, 42], null);
    for (const domain of domains) expect(client.getQueryState([domain, 42])?.isInvalidated).toBe(false);
    reconcile.subscribe(42);

    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);

    for (const domain of domains) {
      await vi.waitFor(() => expect(client.getQueryState([domain, 42])?.isInvalidated).toBe(true));
    }
  });

  it("detaching the last reader aborts the read and blocks a late write", async () => {
    const first = deferred<GatewayReply>();
    defaultAnswer = first.promise;
    const unsubscribe = reconcile.subscribe(42);
    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);

    await vi.waitFor(() => expect(reads).toHaveLength(1));
    unsubscribe();
    expect(reads[0].signal?.aborted).toBe(true);
    first.resolve(resolves(snapshot("fresh")));
    await vi.advanceTimersByTimeAsync(400);
    expect(client.getQueryData(["timeline", 42])).toBeUndefined();
    expect(client.getQueryData(["pending", 42])).toBeUndefined();
  });

  it("requests without a live reader are no-ops", async () => {
    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(reads).toHaveLength(0);
  });
});


describe("reconcile activity for the latest-messages indicator", () => {
  it("stays active across a queued, running and trailing read, then settles", async () => {
    const first = deferred<GatewayReply>();
    const second = deferred<GatewayReply>();
    queuedAnswers = [first.promise, second.promise];
    const activity: boolean[] = [];
    const unwatch = reconcile.watch(() => activity.push(reconcile.isRefreshing(42)));
    const detach = reconcile.subscribe(42);
    expect(reconcile.isRefreshing(42)).toBe(false);
    reconcile.request(42);
    expect(reconcile.isRefreshing(42)).toBe(true);
    expect(reconcile.isRefreshing(43)).toBe(false);
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(reads).toHaveLength(1));
    reconcile.request(42);
    first.resolve(resolves(snapshot("fresh")));
    await vi.advanceTimersByTimeAsync(0);
    expect(reconcile.isRefreshing(42)).toBe(true);
    expect(activity.every(Boolean)).toBe(true);
    await vi.advanceTimersByTimeAsync(200);
    await vi.waitFor(() => expect(reads).toHaveLength(2));
    second.resolve(resolves(snapshot("new")));
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(reconcile.isRefreshing(42)).toBe(false));
    detach(); unwatch();
  });
  it.each(["abort", "detach"])("clears immediately on %s even if the transport ignores cancellation", async (mode) => {
    const fault = new UncancellableFetch();
    rig = httpRig({ base: gateway.origin, fetch: fault.fetch, now: () => performance.now(),
      keys: systemKeySource(), openUpload: noUpload });
    reconcile = createAgentReconcile(client, rig.api);
    const detach = reconcile.subscribe(42);
    reconcile.request(42);
    await vi.advanceTimersByTimeAsync(0);
    expect(reconcile.isRefreshing(42)).toBe(true);
    if (mode === "abort") reconcile.abort(42); else detach();
    expect(reconcile.isRefreshing(42)).toBe(false);
    fault.respond(snapshot("old"));
    await vi.advanceTimersByTimeAsync(0);
    expect(reconcile.isRefreshing(42)).toBe(false);
    if (mode === "abort") detach();
  });
});
