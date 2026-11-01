// meiva:note
// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ApiError } from "./plans";
import { startGateway, OK_JSON, type GatewayReply, type GatewayRequest, type TestGateway } from "./tests/gateway-server";
import {
  httpRig,
  keysWithoutRandomUUID,
  noUpload,
  sequentialKeys,
  steppedClock,
  type HttpRig,
} from "./tests/http-rig";
import { realNetwork } from "./tests/real-network";

let gateway: TestGateway;
let answer: (request: GatewayRequest) => GatewayReply;
let rig: HttpRig;

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function rigWith(keys = sequentialKeys()): HttpRig {
  return httpRig({
    base: gateway.origin,
    fetch: realNetwork.fetch,
    now: steppedClock(1),
    keys,
    openUpload: noUpload,
  });
}

function keyOf(request: GatewayRequest): string | undefined {
  return request.headers["idempotency-key"];
}

const api = () => rig.api;
const requests = () => gateway.requests;
const json = (request: GatewayRequest): unknown => JSON.parse(request.body);

beforeEach(async () => {
  answer = (request) =>
    request.method === "GET" && /^\/api\/agents(?:\?|$)/.test(request.path)
      ? { kind: "json", body: { agents: [], next_cursor: null } }
      : OK_JSON;
  gateway = await startGateway(realNetwork.createServer, (request) => answer(request));
  rig = rigWith();
});

afterEach(async () => {
  await gateway.close();
});

describe("server error surfacing", () => {
  it("joins a FastAPI validation list instead of printing raw JSON", async () => {
    answer = () => ({
      kind: "json",
      status: 422,
      body: { detail: [{ loc: ["body", "harness"], msg: "unknown harness", type: "value_error" }] },
    });
    await expect(api().getAgent(1)).rejects.toThrow("unknown harness");
  });

  it("keeps the server's reason next to its detail", async () => {
    answer = () => ({
      kind: "json",
      status: 400,
      body: {
        detail: "agent 12 runs claude_code; a fork keeps its source's harness",
        reason: "fork_harness_mismatch",
      },
    });
    await expect(api().getAgent(1)).rejects.toThrow(
      "agent 12 runs claude_code; a fork keeps its source's harness (fork_harness_mismatch)",
    );
  });

  it("treats a missing harness route as 'cannot report', not as an error", async () => {
    answer = () => ({ kind: "text", status: 404, body: "not found" });
    await expect(api().getHarnessAvailability("local")).resolves.toEqual([]);
  });

  it("uses an explicit POST to refresh one machine and surfaces probe failures", async () => {
    answer = () => ({ kind: "json", body: [] });
    await expect(api().refreshHarnessAvailability("runner with spaces")).resolves.toEqual([]);
    expect(requests()).toHaveLength(1);
    expect(requests()[0]).toMatchObject({ method: "POST", path: "/api/cluster/machines/runner%20with%20spaces/harnesses/refresh" });
    answer = () => ({ kind: "text", status: 503, body: "runner offline" });
    await expect(api().refreshHarnessAvailability("runner")).rejects.toThrow(/503/);
  });

  it("preserves a committed id and state from a launch-failure response", async () => {
    answer = () => ({
      kind: "json",
      status: 502,
      headers: { "content-type": "application/problem+json" },
      body: {
        reason: "agent_launch_failed",
        detail: "created but launch failed",
        agent_id: 123,
        state: { status: "idling", availability: { reason: "launch_unreachable" } },
        retry_launch_path: "/api/keyed/v1/agents/123/retry-launch",
      },
    });
    const failure = await api().spawnAgent().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({
      agentId: 123,
      retryLaunchPath: "/api/keyed/v1/agents/123/retry-launch",
      launchState: { availability: { reason: "launch_unreachable" } },
    });
  });

  it("patchAgentLabel throws on non-ok response", async () => {
    answer = () => ({ kind: "text", status: 500, body: "nope" });
    await expect(api().patchAgentLabel(5, "x")).rejects.toThrow(/HTTP 500/);
  });
});

describe("read endpoints reach their gateway route", () => {
  const reads: [string, string, (a: HttpRig["api"]) => Promise<unknown>][] = [
    ["getRunTimeline with a window", "/api/agents/405/run-timeline?from=a&to=b", (a) => a.getRunTimeline(405, { from: "a", to: "b" })],
    ["getRunTimeline without a window", "/api/agents/405/run-timeline", (a) => a.getRunTimeline(405)],
    ["getRunTimelineContext", "/api/agents/405/run-timeline/context?at=10&snapshot=v1", (a) => a.getRunTimelineContext(405, 10, "v1")],
    ["getRunTimelineMessages", "/api/agents/405/run-timeline/messages?start=10&end=11&snapshot=v1", (a) => a.getRunTimelineMessages(405, { start: 10, end: 11, snapshot: "v1" })],
    ["getRunTimelineLinks", "/api/insights/run-timeline/links?agents=405%2C6657&from=a&to=b", (a) => a.getRunTimelineLinks([405, 6657], { from: "a", to: "b" })],
    ["getRunTimelineLinkContent", "/api/insights/run-timeline/link-content?notice_id=4", (a) => a.getRunTimelineLinkContent({ notice_id: 4 })],
    ["listAgents defaults to the live roster", "/api/agents?scope=live&limit=100", (a) => a.listAgents()],
    ["listAgents requests terminated history only when explicit", "/api/agents?scope=terminated&limit=100", (a) => a.listAgents({ scope: "terminated" })],
    ["getTimeline with no params", "/api/agents/22/timeline", (a) => a.getTimeline(22)],
    ["getTimeline forwards limit + before", "/api/agents/22/timeline?limit=100&before=12.0", (a) => a.getTimeline(22, { limit: 100, before: "12.0" })],
    ["getTokenUsage", "/api/agents/33/token-usage", (a) => a.getTokenUsage(33)],
    ["getPendingMessages", "/api/agents/33/pending", (a) => a.getPendingMessages(33)],
    ["getCommands is global by default", "/api/commands", (a) => a.getCommands()],
    ["getCommands scopes to an agent", "/api/commands?agent_id=7", (a) => a.getCommands(7)],
    ["getStatsDashboard", "/api/stats/dashboard?hours=24", (a) => a.getStatsDashboard(24)],
    ["getStatsDashboard passes a non-default window", "/api/stats/dashboard?hours=168", (a) => a.getStatsDashboard(168)],
    ["getAlertClasses", "/api/stats/alert-classes?hours=6", (a) => a.getAlertClasses(6)],
    ["getConfig", "/api/config", (a) => a.getConfig()],
    ["getConfig for a machine", "/api/config?machine=runner-1", (a) => a.getConfig("runner-1")],
    ["getResolvedConfig for a model", "/api/config/resolved?model=claude-opus-5", (a) => a.getResolvedConfig("claude-opus-5")],
    ["getResolvedConfig without a model", "/api/config/resolved", (a) => a.getResolvedConfig()],
    ["getMachines", "/api/cluster/machines", (a) => a.getMachines()],
    ["getSystemStatus", "/api/status", (a) => a.getSystemStatus()],
  ];

  it.each(reads)("%s", async (_name, path, call) => {
    await call(api());
    expect(requests()).toHaveLength(1);
    expect(requests()[0].method).toBe("GET");
    expect(requests()[0].path).toBe(path);
  });

  it("listAgents projects every wire lifecycle state to the public model", async () => {
    const statuses = ["running", "idling", "terminated"];
    answer = () => ({
      kind: "json",
      body: {
        agents: statuses.map((status, index) => ({
          agent_id: index + 1,
          status,
          awaiting_response_count: 0,
          highest_notice_priority: null,
        })),
        next_cursor: null,
      },
    });
    const rows = await api().listAgents();
    expect(rows.agents.map((row) => row.status)).toEqual(["running", "idling", "terminated"]);
  });

  it("getAlertClassSamples addresses one class by its full identity and unwraps the samples", async () => {
    answer = () => ({ kind: "json", body: { samples: [{ id: 1 }] } });
    const samples = await api().getAlertClassSamples(
      { level: "error", event_name: "turn failed", source: "runner", process: "" },
      24,
    );
    expect(samples).toEqual([{ id: 1 }]);
    const url = new URL(requests()[0].path, "http://gateway");
    expect(url.pathname).toBe("/api/stats/alert-classes/samples");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      level: "error",
      event_name: "turn failed",
      source: "runner",
      process: "",
      hours: "24",
    });
  });

  it.each(["getTimeline", "getTokenUsage", "getPendingMessages"] as const)(
    "%s carries cancellation to fetch",
    async (method) => {
      const controller = new AbortController();
      if (method === "getTimeline") await api().getTimeline(22, { signal: controller.signal });
      else await api()[method](22, controller.signal);
      expect(rig.sent[0].init.signal?.aborted).toBe(false);
      controller.abort();
      expect(rig.sent[0].init.signal?.aborted).toBe(true);
    },
  );
});

describe("write endpoints", () => {
  it("spawnAgent POSTs JSON body to /api/keyed/v1/agents", async () => {
    await api().spawnAgent({ prompt: "Search X", spawner: "claude-code", prompt_source: "user" });
    const [request] = requests();
    expect([request.method, request.path]).toEqual(["POST", "/api/keyed/v1/agents"]);
    expect(request.headers["content-type"]).toBe("application/json");
    expect(keyOf(request)).toBe("key-1");
    expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    expect(json(request)).toEqual({ prompt: "Search X", spawner: "claude-code", prompt_source: "user" });
  });

  it("spawnAgent with no args POSTs empty body", async () => {
    await api().spawnAgent();
    expect(json(requests()[0])).toEqual({});
  });

  it("retryAgentLaunch POSTs to the existing id", async () => {
    await api().retryAgentLaunch(123, "original-attempt", "launch-retry");
    const [request] = requests();
    expect([request.method, request.path]).toEqual(["POST", "/api/keyed/v1/agents/123/retry-launch"]);
    expect(json(request)).toEqual({ expected_prior_attempt_id: "original-attempt" });
    expect(keyOf(request)).toBe("launch-retry");
  });

  it("terminateAgent POSTs /api/agents/{id}/terminate", async () => {
    await api().terminateAgent(42);
    const [request] = requests();
    expect([request.method, request.path, request.body]).toEqual(["POST", "/api/agents/42/terminate", ""]);
  });

  it("terminateAgent(id, true) POSTs JSON {force:true} for force kill", async () => {
    await api().terminateAgent(42, true);
    const [request] = requests();
    expect([request.method, request.path]).toEqual(["POST", "/api/agents/42/terminate"]);
    expect(json(request)).toEqual({ force: true });
  });

  it("restartAgent POSTs /api/agents/{id}/restart", async () => {
    await api().restartAgent(99, { message: "restarting agent" });
    expect([requests()[0].method, requests()[0].path]).toEqual(["POST", "/api/agents/99/restart"]);
    expect(json(requests()[0])).toEqual({ message: "restarting agent" });
  });

  it("resurrectAgent POSTs JSON {prompt} to /api/agents/{id}/resurrect", async () => {
    await api().resurrectAgent(7, "wake up and resume");
    const [request] = requests();
    expect([request.method, request.path]).toEqual(["POST", "/api/agents/7/resurrect"]);
    expect(json(request)).toEqual({ prompt: "wake up and resume" });
  });

  it("patchAgentLabel PATCHes /api/agents/{id} with label body", async () => {
    await api().patchAgentLabel(5, "New Name");
    const [request] = requests();
    expect([request.method, request.path]).toEqual(["PATCH", "/api/agents/5"]);
    expect(json(request)).toEqual({ label: "New Name" });
  });

  it("cancel and compact post to the agent without a key or a body", async () => {
    await api().cancel(8);
    await api().compact(5);
    expect(requests().map((request) => [request.method, request.path, request.body])).toEqual([
      ["POST", "/api/agents/8/cancel", ""],
      ["POST", "/api/agents/5/compact", ""],
    ]);
    expect(keyOf(requests()[0])).toBeUndefined();
  });

  it("dismissAlertClass POSTs the class identity and reopenAlertClass POSTs the dismissal id", async () => {
    const identity = {
      category: "log",
      level: "warning",
      event_name: "disk_pressure",
      source: "svc",
      process: "gateway",
    } as const;
    await api().dismissAlertClass(identity);
    expect([requests()[0].method, requests()[0].path]).toEqual(["POST", "/api/event-resolutions"]);
    expect(json(requests()[0])).toEqual(identity);

    await api().reopenAlertClass(12);
    expect([requests()[1].method, requests()[1].path]).toEqual(["POST", "/api/event-resolutions/12/reopen"]);
  });

  it("putConfig PUTs JSON body to /api/config and parses the result", async () => {
    answer = () => ({ kind: "json", body: { applied: true, results: {}, restart_required: ["all"] } });
    const result = await api().putConfig({ log_level: "DEBUG" });
    expect([requests()[0].method, requests()[0].path]).toEqual(["PUT", "/api/config"]);
    expect(json(requests()[0])).toEqual({ log_level: "DEBUG" });
    expect(result.applied).toBe(true);
    expect(result.restart_required).toEqual(["all"]);
  });

  it("putConfig appends ?machine= when targeting a host", async () => {
    await api().putConfig({ host_label: "rack-Z" }, "runner-1");
    expect([requests()[0].method, requests()[0].path]).toEqual(["PUT", "/api/config?machine=runner-1"]);
  });

  it("putConfig reports a refusal with the status and the server's text", async () => {
    answer = () => ({ kind: "text", status: 400, body: "bad key" });
    await expect(api().putConfig({ nope: 1 })).rejects.toThrow("HTTP 400: bad key");
  });
});

describe("operation identities", () => {
  it("reuses an explicit restart identity under the principal scope", async () => {
    await api().restartSchedule(17, "same-restart");
    await api().restartSchedule(17, "same-restart");
    for (const request of requests()) {
      expect(keyOf(request)).toBe("same-restart");
      expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    }
  });

  it("gives deliberate new restarts separate identities", async () => {
    await api().restartSchedule(17);
    await api().restartSchedule(17);
    expect(keyOf(requests()[0])).toBe("key-1");
    expect(keyOf(requests()[1])).toBe("key-2");
  });

  it("keeps a supplied creation key and body stable across resubmission", async () => {
    const body = { name: "daily", script: "pass", command: "python schedule.py", enabled: true };
    await api().createSchedule(body, "create-once");
    await api().createSchedule(body, "create-once");
    expect(requests()[0].body).toBe(requests()[1].body);
    for (const request of requests()) {
      expect(keyOf(request)).toBe("create-once");
      expect(request.headers["content-type"]).toBe("application/json");
      expect(request.headers["idempotency-scope"]).toBe("principal-v1");
    }
  });

  it("creation callers reuse a key across calls while new actions mint distinct keys", async () => {
    await api().spawnAgent({}, "creation-a");
    await api().spawnAgent({}, "creation-a");
    await api().spawnAgent();
    await api().spawnAgent();
    const keys = requests().map(keyOf);
    expect(keys.slice(0, 2)).toEqual(["creation-a", "creation-a"]);
    expect(keys[2]).not.toBe(keys[3]);
  });

  it("submits distinct business operations on private HTTP without randomUUID", async () => {
    rig = rigWith(keysWithoutRandomUUID());
    await api().spawnAgent();
    await api().createSchedule({ name: "daily", script: "pass", command: "python schedule.py", enabled: true });
    await api().updateSchedule(17, { enabled: false });
    await api().startSchedule(17);
    await api().stopSchedule(17);
    await api().restartSchedule(17);
    const keys = requests().map(keyOf);
    expect(keys).toHaveLength(6);
    expect(new Set(keys).size).toBe(6);
    for (const key of keys) expect(key).toMatch(UUID_V4);
  });

  it("updates, starts and stops a schedule under keyed requests", async () => {
    await api().updateSchedule(17, { enabled: false }, "u");
    await api().startSchedule(17, "s");
    await api().stopSchedule(17, "x");
    expect(requests().map((request) => [request.method, request.path, keyOf(request)])).toEqual([
      ["PUT", "/api/schedules/17", "u"],
      ["POST", "/api/schedules/17/start", "s"],
      ["POST", "/api/schedules/17/stop", "x"],
    ]);
  });

  it("rejects an unusable supplied key before any request", () => {
    expect(() => api().startSchedule(17, "")).toThrow("idempotency key");
    expect(requests()).toHaveLength(0);
  });
});
