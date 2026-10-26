// meiva:note
// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { startGateway, type GatewayReply, type TestGateway } from "./tests/gateway-server";
import { httpRig, noUpload, sequentialKeys, steppedClock, type HttpRig } from "./tests/http-rig";
import { realNetwork } from "./tests/real-network";

let gateway: TestGateway;
let answer: GatewayReply;
let rig: HttpRig;

beforeEach(async () => {
  answer = { kind: "json", status: 201, body: { status: "idling", inbound_id: 3 } };
  gateway = await startGateway(realNetwork.createServer, () => answer);
  rig = httpRig({
    base: gateway.origin,
    fetch: realNetwork.fetch,
    now: steppedClock(1),
    keys: sequentialKeys(),
    openUpload: noUpload,
  });
});
afterEach(async () => {
  await gateway.close();
});

describe("guarded browser notice resolution", () => {
  it("uses the fixed path, exact global row and explicit principal-scoped key", async () => {
    await rig.api.resolveNotice(7, 11, { action: "answer", reply: "yes" }, "intent");
    const [request] = gateway.requests;
    expect(request.path).toBe("/api/keyed/v1/agents/7/notices/11/resolve");
    expect(request.method).toBe("POST");
    expect(request.headers["idempotency-key"]).toBe("intent");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(JSON.parse(request.body)).toEqual({ action: "answer", reply: "yes" });
  });

  it.each([404, 409, 500])("rejects HTTP %i without fallback or an automatic retry", async (status) => {
    answer = { kind: "json", status, body: { detail: "refused" } };
    await expect(rig.api.resolveNotice(7, 11, { action: "read" }, "intent")).rejects.toMatchObject({ status });
    expect(gateway.requests).toHaveLength(1);
  });

  it("preserves the same key/body/path when a caller explicitly retries a lost response", async () => {
    const body = { action: "read" as const, reply: "yes" };
    answer = { kind: "drop" };
    await expect(rig.api.resolveNotice(7, 11, body, "intent")).rejects.toBeInstanceOf(TypeError);
    expect(gateway.requests).toHaveLength(1);
    answer = { kind: "json", status: 201, body: { status: "idling", inbound_id: 3 } };
    await rig.api.resolveNotice(7, 11, body, "intent");
    expect(gateway.requests[1]).toEqual(gateway.requests[0]);
  });

  it("allocates a different key for separate default-key invocations", async () => {
    await rig.api.resolveNotice(7, 11, { action: "read" });
    await rig.api.resolveNotice(7, 11, { action: "read" });
    const keys = gateway.requests.map((request) => request.headers["idempotency-key"]);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });
});
