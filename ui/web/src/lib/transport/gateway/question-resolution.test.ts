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

const body = {
  expected_ids: [11, 12],
  results: [
    { id: 11, answer: { text: "yes" }, skip: false },
    { id: 12, skip: true },
  ],
};

describe("guarded browser question submission", () => {
  it("uses the fixed path, the agent's whole open set and an explicit principal-scoped key", async () => {
    await expect(rig.api.answerQuestions(7, body, "intent")).resolves.toEqual({ kind: "settled" });
    const [request] = gateway.requests;
    expect(request.path).toBe("/api/keyed/v1/agents/7/questions/answers");
    expect(request.method).toBe("POST");
    expect(request.headers["idempotency-key"]).toBe("intent");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(JSON.parse(request.body)).toEqual(body);
  });

  it("returns the latest open set of a changed submission instead of an error", async () => {
    const open = [{ id: 11, agent_id: 7, title: "A?" }];
    answer = { kind: "json", status: 409, body: { detail: "changed", open } };
    await expect(rig.api.answerQuestions(7, body, "intent")).resolves.toEqual({ kind: "changed", open });
    expect(gateway.requests).toHaveLength(1);
  });

  it("treats a 409 without an open set (a key reused with different content) as an error", async () => {
    answer = { kind: "json", status: 409, body: { detail: "idempotency key identifies a different keyed operation" } };
    await expect(rig.api.answerQuestions(7, body, "intent")).rejects.toMatchObject({ status: 409 });
    expect(gateway.requests).toHaveLength(1);
  });

  it.each([404, 422, 500])("rejects HTTP %i without fallback or an automatic retry", async (status) => {
    answer = { kind: "json", status, body: { detail: "refused" } };
    await expect(rig.api.answerQuestions(7, body, "intent")).rejects.toMatchObject({ status });
    expect(gateway.requests).toHaveLength(1);
  });

  it("preserves the same key/body/path when a caller explicitly retries a lost response", async () => {
    answer = { kind: "drop" };
    await expect(rig.api.answerQuestions(7, body, "intent")).rejects.toBeInstanceOf(TypeError);
    expect(gateway.requests).toHaveLength(1);
    answer = { kind: "json", status: 201, body: { status: "idling", inbound_id: 3 } };
    await rig.api.answerQuestions(7, body, "intent");
    expect(gateway.requests[1]).toEqual(gateway.requests[0]);
  });

  it("allocates a different key for separate default-key invocations", async () => {
    await rig.api.answerQuestions(7, body);
    await rig.api.answerQuestions(7, body);
    const keys = gateway.requests.map((request) => request.headers["idempotency-key"]);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });
});

describe("advancing an agent's seen position", () => {
  it("posts the id bound to the agent's own path with the caller's principal-scoped key", async () => {
    answer = { kind: "json", status: 200, body: { seen_id: 41, advanced: true } };
    await expect(rig.api.markUserMessagesSeen(7, { up_to_id: 41 }, "seen-intent")).resolves.toEqual({ seen_id: 41, advanced: true });
    const [request] = gateway.requests;
    expect(request.path).toBe("/api/keyed/v1/agents/7/user-messages/seen");
    expect(request.method).toBe("POST");
    expect(request.headers["idempotency-key"]).toBe("seen-intent");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(JSON.parse(request.body)).toEqual({ up_to_id: 41 });
  });

  it("rejects a refusal without an automatic retry", async () => {
    answer = { kind: "json", status: 404, body: { detail: "agent 7 does not exist" } };
    await expect(rig.api.markUserMessagesSeen(7, { up_to_id: 41 }, "seen-intent")).rejects.toMatchObject({ status: 404 });
    expect(gateway.requests).toHaveLength(1);
  });
});
