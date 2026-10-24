import { describe, expect, it } from "vitest";

import {
  ApiError,
  confirmDraftReceipt,
  errorFromResponse,
  hasHttpStatus,
  isAmbiguousDeliveryError,
} from "./response-plan";

describe("server error surfacing", () => {
  it("joins a FastAPI validation list instead of printing raw JSON", () => {
    const error = errorFromResponse(422, JSON.stringify({
      detail: [{ loc: ["body", "harness"], msg: "unknown harness", type: "value_error" }, { msg: "second" }, "stray"],
    }));
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(422);
    expect(error.message).toBe("unknown harness; second");
  });

  it("keeps the server's reason next to its detail", () => {
    const error = errorFromResponse(400, JSON.stringify({
      detail: "agent 12 runs claude_code; a fork keeps its source's harness",
      reason: "fork_harness_mismatch",
    }));
    expect(error.message).toBe(
      "agent 12 runs claude_code; a fork keeps its source's harness (fork_harness_mismatch)",
    );
  });

  it("falls back to the status and the raw text when the body is not an object", () => {
    expect(errorFromResponse(404, "not found").message).toBe("HTTP 404: not found");
    expect(errorFromResponse(500, "").message).toBe("HTTP 500: request failed");
    expect(errorFromResponse(502, "[1,2]").message).toBe("HTTP 502: [1,2]");
  });

  it("uses a structured detail's own code and message", () => {
    const error = errorFromResponse(409, JSON.stringify({ detail: { code: "busy", message: "agent is busy" } }));
    expect(error.code).toBe("busy");
    expect(error.message).toBe("agent is busy");
  });

  it("preserves a committed id and state from a launch-failure response", () => {
    const error = errorFromResponse(502, JSON.stringify({
      reason: "agent_launch_failed",
      detail: "created but launch failed",
      agent_id: 123,
      state: { status: "idling", availability: { reason: "launch_unreachable" } },
      retry_launch_path: "/api/keyed/v1/agents/123/retry-launch",
    }));
    expect(error.agentId).toBe(123);
    expect(error.launchState?.availability?.reason).toBe("launch_unreachable");
    expect(error.retryLaunchPath).toBe("/api/keyed/v1/agents/123/retry-launch");
    expect(error.message).toBe("created but launch failed (agent_launch_failed)");
  });
});

describe("delivery classification", () => {
  it.each([
    [new ApiError(408, "timeout"), true],
    [new ApiError(429, "slow down"), true],
    [new ApiError(500, "boom"), true],
    [new ApiError(503, "paused"), true],
    [new ApiError(400, "bad"), false],
    [new ApiError(404, "gone"), false],
    [new ApiError(409, "conflict"), false],
    [new TypeError("fetch failed"), true],
    ["response lost", true],
  ] as const)("treats %j as ambiguous=%s", (error, ambiguous) => {
    expect(isAmbiguousDeliveryError(error)).toBe(ambiguous);
  });

  it("recognizes an error carrying a given HTTP status", () => {
    expect(hasHttpStatus(new ApiError(404, "x"), 404)).toBe(true);
    expect(hasHttpStatus(new ApiError(500, "x"), 404)).toBe(false);
    expect(hasHttpStatus(new Error("x"), 404)).toBe(false);
    expect(hasHttpStatus(null, 404)).toBe(false);
  });
});

describe("draft receipt", () => {
  it("accepts a 200 receipt naming a positive integer agent", () => {
    expect(confirmDraftReceipt(200, { agent_id: 42 })).toEqual({ agent_id: 42 });
  });

  it.each([null, {}, { agent_id: "42" }, { agent_id: 0 }, { agent_id: -1 }, { agent_id: 1.5 }])(
    "rejects an unconfirmed receipt %j", (receipt) => {
      expect(() => confirmDraftReceipt(200, receipt)).toThrow("acceptance is unconfirmed");
    },
  );

  it("rejects an unexpected successful status", () => {
    expect(() => confirmDraftReceipt(201, { agent_id: 42 })).toThrow("acceptance is unconfirmed");
  });
});
