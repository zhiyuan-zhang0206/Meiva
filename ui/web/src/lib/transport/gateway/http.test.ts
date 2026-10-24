// meiva:note
// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { startGateway, OK_JSON, type GatewayReply, type TestGateway } from "./tests/gateway-server";
import { httpRig, noUpload, scriptedClock, sequentialKeys, steppedClock, type HttpRig } from "./tests/http-rig";
import { realNetwork } from "./tests/real-network";

let gateway: TestGateway;
let answer: GatewayReply;

function rigWith(now: () => number): HttpRig {
  return httpRig({
    base: gateway.origin,
    fetch: realNetwork.fetch,
    now,
    keys: sequentialKeys(),
    openUpload: noUpload,
  });
}

beforeEach(async () => {
  answer = OK_JSON;
  gateway = await startGateway(() => answer);
});

afterEach(async () => {
  vi.useRealTimers();
  await gateway.close();
});

describe("one gateway request", () => {
  it("is sent to the gateway origin with the session credential", async () => {
    const rig = rigWith(steppedClock(1));
    await rig.http.send("/api/status");
    expect(gateway.requests.map((request) => [request.method, request.path])).toEqual([["GET", "/api/status"]]);
    expect(rig.sent[0].url).toBe(`${gateway.origin}/api/status`);
    expect(rig.sent[0].init.credentials).toBe("include");
  });

  it("keeps the caller's method, headers and body", async () => {
    const rig = rigWith(steppedClock(1));
    await rig.http.send("/api/x", { method: "PUT", headers: { "x-test": "1" }, body: "payload" });
    expect(gateway.requests[0]).toMatchObject({ method: "PUT", body: "payload" });
    expect(gateway.requests[0].headers["x-test"]).toBe("1");
  });

  it("decodes a JSON body", async () => {
    answer = { kind: "json", body: { status: "ok" } };
    const rig = rigWith(steppedClock(1));
    await expect(rig.http.send("/api/status").then(rig.http.ok)).resolves.toEqual({ status: "ok" });
  });

  it("turns an HTTP failure into an ApiError carrying the status", async () => {
    answer = { kind: "json", status: 422, body: { detail: [{ msg: "unknown harness" }] } };
    const rig = rigWith(steppedClock(1));
    await expect(rig.http.send("/api/x").then(rig.http.ok)).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      message: "unknown harness",
    });
  });

  it("builds absolute urls against the configured origin", () => {
    expect(rigWith(steppedClock(1)).http.url("/api/a")).toBe(`${gateway.origin}/api/a`);
  });

  it("mints operation keys from the transport's key source", () => {
    const rig = rigWith(steppedClock(1));
    expect(rig.http.newOperationKey()).toBe("key-1");
    expect(rig.http.newOperationKey()).toBe("key-2");
  });
});

describe("browser API timing", () => {
  it("reports a slow request with query strings removed and numeric segments collapsed", async () => {
    const rig = rigWith(scriptedClock([1_000, 1_801]));
    await rig.api.getAgentInspectStatistics(2697, 1);
    expect(rig.slow).toEqual([{ key: "agents.id.inspect.statistics", value: 801, dedupe: false }]);
  });

  it("does not report a request at the 800 ms threshold", async () => {
    const rig = rigWith(scriptedClock([1_000, 1_800]));
    await rig.api.head("/api/status");
    expect(rig.slow).toEqual([]);
  });

  it("does not time the frontend telemetry endpoint", async () => {
    const rig = rigWith(scriptedClock([1_000, 2_000]));
    await rig.api.head("/api/frontend-telemetry");
    expect(rig.slow).toEqual([]);
  });

  it("reports slow fetch failures before preserving the rejection", async () => {
    answer = { kind: "drop" };
    const rig = rigWith(scriptedClock([1_000, 1_900]));
    await expect(rig.api.head("/api/status")).rejects.toBeInstanceOf(TypeError);
    expect(rig.slow).toEqual([{ key: "status", value: 900, dedupe: false }]);
  });
});

describe("bounded reads", () => {
  it("getAgentInspectStatistics propagates caller cancellation to its bounded request", async () => {
    answer = { kind: "hang" };
    const rig = rigWith(steppedClock(1));
    const controller = new AbortController();
    const pending = rig.api.getAgentInspectStatistics(7, 24, controller.signal).catch((error: unknown) => error);
    expect(rig.sent[0].url).toBe(`${gateway.origin}/api/agents/7/inspect/statistics?hours=24`);
    expect(rig.sent[0].init.signal).not.toBe(controller.signal);
    expect(rig.sent[0].init.signal?.aborted).toBe(false);
    controller.abort();
    expect(rig.sent[0].init.signal?.aborted).toBe(true);
    expect(await pending).toMatchObject({ name: "AbortError" });
  });

  it("getAgentInspectLive GETs the cheap endpoint and propagates cancellation", async () => {
    answer = { kind: "hang" };
    const rig = rigWith(steppedClock(1));
    const controller = new AbortController();
    const pending = rig.api.getAgentInspectLive(7, controller.signal).catch((error: unknown) => error);
    expect(rig.sent[0].url).toBe(`${gateway.origin}/api/agents/7/inspect/live`);
    expect(rig.sent[0].init.signal?.aborted).toBe(false);
    controller.abort();
    expect(rig.sent[0].init.signal?.aborted).toBe(true);
    expect(await pending).toMatchObject({ name: "AbortError" });
  });

  it("getAgentInspectStatistics aborts fetch and rejects when its response deadline expires", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    answer = { kind: "hang" };
    const rig = rigWith(steppedClock(1));
    const timedOut = expect(rig.api.getAgentInspectStatistics(7)).rejects.toThrow("Inspector request exceeded 35000ms");
    await vi.advanceTimersByTimeAsync(35_000);
    await timedOut;
    expect(rig.sent[0].init.signal?.aborted).toBe(true);
  });

  it("uses the same 35 second budget for the initial range", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    answer = { kind: "hang" };
    const rig = rigWith(steppedClock(1));
    const failure = rig.api.getRunTimeline(6111, { recentHours: 24, maxUnits: 5000 }).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(34_999);
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(await failure).toMatchObject({ name: "TimeoutError" });
  });

  it.each(["fetch", "body"] as const)("bounds a pending %s and aborts its network request", async (phase) => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    answer = phase === "fetch" ? { kind: "hang" } : { kind: "headers-then-hang" };
    const rig = rigWith(steppedClock(1));
    const failure = rig.api.getRunTimeline(6111).catch((error: unknown) => error);
    if (phase === "body") await vi.waitFor(() => expect(rig.responses).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(35_000);
    expect(await failure).toMatchObject({ name: "TimeoutError" });
    expect(rig.sent[0].init.signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("forwards caller cancellation to a pending fetch and preserves AbortError", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    answer = { kind: "hang" };
    const rig = rigWith(steppedClock(1));
    const caller = new AbortController();
    const result = rig.api.getRunTimeline(6111, { signal: caller.signal }).catch((error: unknown) => error);
    caller.abort();
    expect(await result).toMatchObject({ name: "AbortError" });
    expect(rig.sent[0].init.signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps caller cancellation connected while consuming the response body", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    answer = { kind: "headers-then-hang" };
    const rig = rigWith(steppedClock(1));
    const caller = new AbortController();
    const result = rig.api.getRunTimeline(6111, { signal: caller.signal }).catch((error: unknown) => error);
    await vi.waitFor(() => expect(rig.responses).toHaveLength(1));
    caller.abort();
    expect(await result).toMatchObject({ name: "AbortError" });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aborts at once for a caller signal that is already aborted", async () => {
    answer = { kind: "hang" };
    const rig = rigWith(steppedClock(1));
    const caller = new AbortController();
    caller.abort();
    const result = await rig.api.getRunTimeline(6111, { signal: caller.signal }).catch((error: unknown) => error);
    expect(result).toMatchObject({ name: "AbortError" });
    expect(rig.sent[0].init.signal?.aborted).toBe(true);
  });
});
