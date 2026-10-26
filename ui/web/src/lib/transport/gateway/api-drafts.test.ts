// meiva:note
// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Api } from "./api-factory";
import { startGateway, type GatewayReply, type TestGateway } from "./tests/gateway-server";
import { httpRig, noUpload, sequentialKeys, steppedClock, type HttpRig } from "./tests/http-rig";
import { realNetwork } from "./tests/real-network";

const drafts = [
  { name: "Guide", path: "/api/keyed/v1/guide/draft", body: { nl: "operate the cluster" },
    submit: (api: Api, key?: string) => api.draftGuide("operate the cluster", key) },
  { name: "Schedule Maker", path: "/api/keyed/v1/schedules/draft", body: { nl: "run nightly" },
    submit: (api: Api, key?: string) => api.draftSchedule("run nightly", key) },
  { name: "Package Installer", path: "/api/keyed/v1/packages/draft", body: { kind: "mcp", nl: "install tools" },
    submit: (api: Api, key?: string) => api.draftPackage("mcp", "install tools", key) },
];

let gateway: TestGateway;
let answer: GatewayReply;
let rig: HttpRig;

beforeEach(async () => {
  answer = { kind: "json", body: { agent_id: 42 } };
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

describe.each(drafts)("$name guarded draft", ({ path, body, submit }) => {
  it("submits the immutable intent and scoped key with the session credential", async () => {
    expect(await submit(rig.api, "intent-1")).toEqual({ agent_id: 42 });
    expect(gateway.requests).toHaveLength(1);
    const [request] = gateway.requests;
    expect([request.method, request.path]).toEqual(["POST", path]);
    expect(rig.sent[0].init.credentials).toBe("include");
    expect(JSON.parse(request.body)).toEqual(body);
    expect(request.headers["idempotency-key"]).toBe("intent-1");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(request.headers["content-type"]).toBe("application/json");
  });

  it("preserves one explicit key and request after a lost response", async () => {
    answer = { kind: "drop" };
    await expect(submit(rig.api, "intent-1")).rejects.toBeInstanceOf(TypeError);
    expect(gateway.requests).toHaveLength(1);
    answer = { kind: "json", body: { agent_id: 42 } };
    await submit(rig.api, "intent-1");
    expect(gateway.requests).toHaveLength(2);
    const [first, second] = gateway.requests;
    expect(second).toEqual(first);
  });

  it("uses distinct keys for deliberately new submissions", async () => {
    await submit(rig.api);
    await submit(rig.api);
    const keys = gateway.requests.map((request) => request.headers["idempotency-key"]);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });

  it.each([404, 409, 500])("surfaces HTTP %i without a retry", async (status) => {
    answer = { kind: "json", status, body: { detail: "refused" } };
    await expect(submit(rig.api, "intent-1")).rejects.toThrow("refused");
    expect(gateway.requests).toHaveLength(1);
    expect(gateway.requests[0].path).toBe(path);
  });

  it.each([null, {}, { agent_id: "42" }, { agent_id: 0 }, { agent_id: -1 }, { agent_id: 1.5 }])(
    "rejects an unconfirmed receipt %j", async (receipt) => {
      answer = { kind: "json", body: receipt };
      await expect(submit(rig.api, "intent-1")).rejects.toThrow("acceptance is unconfirmed");
      expect(gateway.requests).toHaveLength(1);
    },
  );

  it("rejects an unexpected successful status without resending", async () => {
    answer = { kind: "json", status: 201, body: { agent_id: 42 } };
    await expect(submit(rig.api, "intent-1")).rejects.toThrow("acceptance is unconfirmed");
    expect(gateway.requests).toHaveLength(1);
  });

  it.each(["", "x".repeat(129)])("rejects invalid keys before HTTP", async (key) => {
    await expect(submit(rig.api, key)).rejects.toThrow("idempotency key");
    expect(gateway.requests).toHaveLength(0);
  });
});
