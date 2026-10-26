// meiva:note
// @vitest-environment node
import { afterEach, beforeEach, expect, it } from "vitest";

import { startGateway, type GatewayReply, type TestGateway } from "./tests/gateway-server";
import { httpRig, noUpload, sequentialKeys, steppedClock, type HttpRig } from "./tests/http-rig";
import { realNetwork } from "./tests/real-network";

let gateway: TestGateway;
let answer: GatewayReply;
let rig: HttpRig;

beforeEach(async () => {
  answer = { kind: "json", body: {} };
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

it("keeps the exact launch retry intent after a lost response", async () => {
  const submit = () => rig.api.retryAgentLaunch(8, "prior-attempt", "fixed-intent");
  answer = { kind: "drop" };
  await expect(submit()).rejects.toBeInstanceOf(TypeError);
  expect(gateway.requests).toHaveLength(1);
  answer = { kind: "json", body: {} };
  await submit();
  expect(gateway.requests).toHaveLength(2);
  const [first, replay] = gateway.requests;
  expect(first.path).toBe("/api/keyed/v1/agents/8/retry-launch");
  expect(replay.path).toBe(first.path);
  expect(first.method).toBe("POST");
  expect(JSON.parse(first.body)).toEqual({ expected_prior_attempt_id: "prior-attempt" });
  expect(replay.body).toBe(first.body);
  expect(replay.headers["idempotency-key"]).toBe("fixed-intent");
  expect(replay.headers["idempotency-scope"]).toBe("principal-v1");
});

it.each(["", "x".repeat(129)])("rejects invalid operation keys before sending", (key) => {
  expect(() => rig.api.retryAgentLaunch(8, "prior-attempt", key)).toThrow("idempotency key");
  expect(gateway.requests).toHaveLength(0);
});
