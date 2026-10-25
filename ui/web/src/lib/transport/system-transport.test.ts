// meiva:note
// @vitest-environment happy-dom

import { describe, expect, it } from "vitest";

import { newOperationKey } from "./operation-key";
import { operationKeyFrom } from "./gateway/plans";
import { systemHttpTransport, systemKeySource, systemStreamTransport } from "./system-transport";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

describe("system transport over the platform's own globals", () => {
  it("mints distinct operation keys from the platform's randomness", () => {
    const first = newOperationKey();
    expect(first).toMatch(UUID);
    expect(newOperationKey()).not.toBe(first);
    expect(operationKeyFrom(systemKeySource())).toMatch(UUID);
  });

  it("times requests on a monotonic clock", () => {
    const transport = systemHttpTransport();
    const first = transport.now();
    expect(transport.now()).toBeGreaterThanOrEqual(first);
  });

  it("hands the key source to the gateway client", () => {
    const keys = systemHttpTransport().keys;
    expect(operationKeyFrom(keys)).toMatch(UUID);
  });

  it("names each tab apart from the others", () => {
    const transport = systemStreamTransport();
    expect(transport.newTabId()).not.toBe(transport.newTabId());
  });

  it("reads the wall clock for interest freshness", () => {
    const transport = systemStreamTransport();
    expect(Math.abs(transport.wallNow() - Date.now())).toBeLessThan(1_000);
  });

  it("describes the page it runs in", () => {
    const { facts, page } = systemStreamTransport();
    expect(typeof facts.secureContext).toBe("boolean");
    expect(typeof facts.locksObject).toBe("boolean");
    expect(page.visibilityState).toBe(document.visibilityState);
  });

  it("offers tab messaging only where the platform has BroadcastChannel", () => {
    const { openChannel } = systemStreamTransport();
    expect(openChannel === null).toBe(typeof BroadcastChannel === "undefined");
  });

  it("offers locks only where the platform has a usable lock manager", () => {
    const { locks } = systemStreamTransport();
    const platform: unknown = Reflect.get(navigator, "locks");
    const usable = typeof platform === "object" && platform !== null && "request" in platform &&
      typeof platform.request === "function";
    expect(locks !== null).toBe(usable);
  });
});
