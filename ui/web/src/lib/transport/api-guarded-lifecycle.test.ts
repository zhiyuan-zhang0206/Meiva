import { afterEach, expect, it, vi } from "vitest";

import { api } from "./api";

afterEach(() => vi.unstubAllGlobals());

it.each([
  ["launch retry", "/retry-launch", { expected_prior_attempt_id: "prior-attempt" },
    () => api.retryAgentLaunch(8, "prior-attempt", "fixed-intent")],
] as const)("keeps the exact %s intent after a lost response", async (_name, suffix, body, submit) => {
  const fetch = vi.fn().mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValue(new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  await expect(submit()).rejects.toThrow("response lost");
  expect(fetch).toHaveBeenCalledTimes(1);
  await submit();
  expect(fetch).toHaveBeenCalledTimes(2);
  const [url, first] = fetch.mock.calls[0] as [string, RequestInit];
  const [replayUrl, replay] = fetch.mock.calls[1] as [string, RequestInit];
  expect(url).toMatch(new RegExp(`/api/keyed/v1/agents/8${suffix}$`));
  expect(replayUrl).toBe(url);
  expect(first.method).toBe("POST");
  expect(JSON.parse(first.body as string)).toEqual(body);
  expect(replay.body).toBe(first.body);
  expect(new Headers(replay.headers).get("Idempotency-Key")).toBe("fixed-intent");
  expect(new Headers(replay.headers).get("Idempotency-Scope")).toBe("principal-v1");
});

it.each(["", "x".repeat(129)])("rejects invalid operation keys before sending", (key) => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  expect(() => api.retryAgentLaunch(8, "prior-attempt", key)).toThrow("idempotency key");
  expect(fetch).not.toHaveBeenCalled();
});
