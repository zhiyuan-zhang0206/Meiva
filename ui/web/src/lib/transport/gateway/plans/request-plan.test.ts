import { describe, expect, it } from "vitest";

import { keyedJson, keyedPost, patchJson, postInit, postJson, putJson } from "./request-plan";

describe("request plans", () => {
  it("a bare POST carries no body and no key", () => {
    expect(postInit).toEqual({ method: "POST" });
    expect(Object.isFrozen(postInit)).toBe(true);
  });

  it.each([
    ["POST", postJson],
    ["PATCH", patchJson],
    ["PUT", putJson],
  ] as const)("a %s with a body is JSON and carries no operation key", (method, plan) => {
    expect(plan({ label: "x" })).toEqual({
      method,
      headers: { "content-type": "application/json" },
      body: '{"label":"x"}',
    });
  });

  it("a keyed JSON write pairs the body with the principal-scoped key", () => {
    expect(keyedJson({ a: 1 }, "intent")).toEqual({
      method: "POST",
      headers: {
        "content-type": "application/json",
        "Idempotency-Key": "intent",
        "Idempotency-Scope": "principal-v1",
      },
      body: '{"a":1}',
    });
  });

  it("a keyed write can be a PUT", () => {
    expect(keyedJson({}, "intent", "PUT").method).toBe("PUT");
  });

  it("a keyed POST without a body carries only the key headers", () => {
    expect(keyedPost("restart-1")).toEqual({
      method: "POST",
      headers: { "Idempotency-Key": "restart-1", "Idempotency-Scope": "principal-v1" },
    });
  });

  it.each(["", "x".repeat(129)])("rejects the unusable key %j before building a plan", (key) => {
    expect(() => keyedJson({}, key)).toThrow("idempotency key");
    expect(() => keyedPost(key)).toThrow("idempotency key");
  });

  it("does not share state between plans", () => {
    const first = postJson({ a: 1 });
    const second = postJson({ a: 2 });
    expect(first.body).toBe('{"a":1}');
    expect(second.body).toBe('{"a":2}');
    expect(postInit).toEqual({ method: "POST" });
  });
});
