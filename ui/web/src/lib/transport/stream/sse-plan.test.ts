import { describe, expect, it } from "vitest";

import { probeVerdict, readyStateKind, reconnectDelay, SSE_INIT } from "./sse-plan";

describe("reconnect delay", () => {
  it.each([
    [0, 1_000],
    [1, 2_000],
    [2, 4_000],
    [4, 16_000],
    [5, 30_000],
    [20, 30_000],
  ])("after %i consecutive failures waits %i ms", (failures, expected) => {
    expect(reconnectDelay(failures, 1_000, 30_000)).toBe(expected);
  });
});

describe("EventSource ready state", () => {
  it("maps the three platform states", () => {
    expect(readyStateKind(0)).toBe("connecting");
    expect(readyStateKind(1)).toBe("open");
    expect(readyStateKind(2)).toBe("closed");
  });

  it("fails fast on a state the platform does not define", () => {
    expect(() => readyStateKind(99)).toThrow("unknown EventSource readyState: 99");
  });
});

describe("session probe verdict", () => {
  it("stops on an invalid session and retries on a valid one", () => {
    expect(probeVerdict({ authenticated: false })).toBe("invalid-session");
    expect(probeVerdict({ authenticated: true })).toBe("reopen");
  });
});

describe("event source options", () => {
  it("always asks the browser for the session cookie", () => {
    expect(SSE_INIT).toEqual({ withCredentials: true });
  });
});
