// meiva:note
// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MessageDeliveryUnknownError } from "./plans";
import { startGateway, type GatewayReply, type GatewayRequest, type TestGateway } from "./tests/gateway-server";
import { httpRig, noUpload, sequentialKeys, steppedClock, type HttpRig } from "./tests/http-rig";
import { realNetwork } from "./tests/real-network";

let gateway: TestGateway;
let answer: (request: GatewayRequest, ordinal: number) => GatewayReply;
let rig: HttpRig;

const RECEIPT: GatewayReply = { kind: "json", body: { status: "running", inbound_id: 77 } };
const MISSING: GatewayReply = { kind: "text", status: 404, body: "not found" };

const isReconcile = (request: GatewayRequest) => request.path.endsWith("/reconcile");

async function settle<T>(delivery: Promise<T>): Promise<void> {
  const progress = { settled: false };
  void delivery.then(() => { progress.settled = true; }, () => { progress.settled = true; });
  for (let turn = 0; turn < 400 && !progress.settled; turn += 1) {
    await vi.advanceTimersByTimeAsync(250);
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
}

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  answer = () => RECEIPT;
  gateway = await startGateway((request) => answer(request, gateway.requests.length - 1));
  rig = httpRig({
    base: gateway.origin,
    fetch: realNetwork.fetch,
    now: steppedClock(1),
    keys: sequentialKeys(),
    openUpload: noUpload,
  });
});

afterEach(async () => {
  vi.useRealTimers();
  await gateway.close();
});

describe("sendMessage", () => {
  it("POSTs one stable client message id with the message", async () => {
    await rig.api.sendMessage(3, "hello", "client-message-123");
    const [request] = gateway.requests;
    expect([request.method, request.path]).toEqual(["POST", "/api/agents/3/messages"]);
    expect(request.headers["content-type"]).toBe("application/json");
    expect(request.headers["idempotency-key"]).toBe("client-message-123");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(JSON.parse(request.body)).toEqual({ content: "hello", source: "user" });
  });

  it("a definite refusal ends the send without a reconcile", async () => {
    answer = () => ({ kind: "json", status: 409, body: { detail: "agent is terminated" } });
    await expect(rig.api.sendMessage(3, "hello", "client-refused")).rejects.toMatchObject({ status: 409 });
    expect(gateway.requests).toHaveLength(1);
  });

  it("a timed-out POST reconciles by the same client message id", async () => {
    answer = (request) => (isReconcile(request) ? RECEIPT : { kind: "hang" });
    const delivery = rig.api.sendMessage(3, "hello", "client-timeout-123");
    await vi.waitFor(() => expect(gateway.requests).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(10_000);

    await expect(delivery).resolves.toEqual({ status: "running", inbound_id: 77 });
    expect(gateway.requests.map((request) => request.path)).toEqual([
      "/api/agents/3/messages",
      "/api/agents/3/messages/reconcile",
    ]);
    for (const request of gateway.requests) {
      expect(request.headers["idempotency-key"]).toBe("client-timeout-123");
      expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    }
  });

  it("times out when headers arrive but the response body never completes", async () => {
    answer = (request) => (isReconcile(request) ? RECEIPT : { kind: "headers-then-hang" });
    const delivery = rig.api.sendMessage(3, "body hangs", "client-body-timeout");
    await vi.waitFor(() => expect(rig.responses).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(10_000);

    await expect(delivery).resolves.toEqual({ status: "running", inbound_id: 77 });
    expect(gateway.requests[1].path).toMatch(/\/messages\/reconcile$/);
  });

  it("a missing receipt resends the original body with the same id", async () => {
    answer = (request, ordinal) => {
      if (ordinal === 0) return { kind: "drop" };
      if (isReconcile(request)) return MISSING;
      return { kind: "json", status: 201, body: { status: "running", inbound_id: 88 } };
    };
    await expect(rig.api.sendMessage(3, "retry me", "client-retry-123")).resolves.toEqual({
      status: "running",
      inbound_id: 88,
    });
    expect(gateway.requests.map((request) => request.path)).toEqual([
      "/api/agents/3/messages",
      "/api/agents/3/messages/reconcile",
      "/api/agents/3/messages",
    ]);
    expect(gateway.requests.map((request) => request.headers["idempotency-key"])).toEqual([
      "client-retry-123",
      "client-retry-123",
      "client-retry-123",
    ]);
    expect(gateway.requests[2].body).toBe(gateway.requests[0].body);
  });

  it("keeps a post-resend 404 ambiguous through the reconciliation deadline", async () => {
    answer = (request) => (isReconcile(request) ? MISSING : { kind: "drop" });
    const delivery = rig.api.sendMessage(3, "still uncertain", "client-uncertain-123");
    const assertion = expect(delivery).rejects.toBeInstanceOf(MessageDeliveryUnknownError);
    await settle(delivery);
    await assertion;

    expect(gateway.requests.map((request) => request.path)).toEqual([
      "/api/agents/3/messages",
      "/api/agents/3/messages/reconcile",
      "/api/agents/3/messages",
      "/api/agents/3/messages/reconcile",
      "/api/agents/3/messages/reconcile",
    ]);
  });

  it("names the unconfirmed message so the caller keeps its draft", async () => {
    answer = () => ({ kind: "drop" });
    const delivery = rig.api.sendMessage(3, "lost", "client-lost-1");
    const failure = delivery.catch((error: unknown) => error);
    await settle(delivery.catch(() => undefined));
    expect(await failure).toMatchObject({
      name: "MessageDeliveryUnknownError",
      clientMessageId: "client-lost-1",
    });
  });

  it("reconciles up to three times before giving up", async () => {
    answer = () => ({ kind: "drop" });
    const delivery = rig.api.sendMessage(3, "lost", "client-lost-2");
    const failure = delivery.catch((error: unknown) => error);
    await settle(delivery.catch(() => undefined));
    await failure;
    expect(gateway.requests.filter(isReconcile)).toHaveLength(3);
    expect(gateway.requests.filter((request) => !isReconcile(request))).toHaveLength(1);
  });
});
