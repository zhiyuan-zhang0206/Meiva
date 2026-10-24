import { describe, expect, it, vi } from "vitest";

import { sharedSseSupported, StreamHub, transportModeReport } from "./stream-hub";
import { FakeNetwork, FakeTab } from "./tests/fake-stream";
import { ScriptedAuth } from "./tests/scripted-auth";

function hubOver(capability: "shared" | "legacy", secureContext = true) {
  const fake = new FakeTab({ network: new FakeNetwork(), capability, wallNow: () => 0, secureContext });
  const auth = new ScriptedAuth();
  const hub = new StreamHub(fake.transport, { checkAuth: auth.checkAuth, sessionInvalid: () => undefined }, "http://gateway.test");
  return { fake, hub };
}

describe("stream hub", () => {
  it("hands every provider the same unmerged transport", () => {
    const { hub } = hubOver("shared");
    const first = hub.transportFor([]);
    expect(first).not.toBeNull();
    expect(hub.transportFor([])).toBe(first);
    hub.dispose();
  });

  it("without profile APIs the unmerged case keeps each page on its own EventSource", () => {
    const { hub } = hubOver("legacy");
    expect(hub.transportFor([])).toBeNull();
  });

  it("providers asking for the same channel set share one transport", () => {
    const { hub } = hubOver("shared");
    const first = hub.transportFor(["system", "alerts"]);
    expect(hub.transportFor(["system", "alerts"])).toBe(first);
    expect(hub.transportFor(["system"])).not.toBe(first);
    hub.dispose();
  });

  it("a merged transport owns its source alone when the page has no lock manager", async () => {
    vi.useFakeTimers();
    try {
      const { hub, fake } = hubOver("legacy");
      const transport = hub.transportFor(["system", "systemAll", "alerts"]);
      expect(transport).not.toBeNull();
      const unsubscribe = transport?.subscribe("system", { onFrame: () => undefined, onState: () => undefined });
      await Promise.resolve();
      expect(transport?.isLeader()).toBe(true);
      expect(fake.sources).toHaveLength(1);
      expect(fake.sources[0].url).toMatch(/^http:\/\/gateway\.test\/api\/stream\?channels=global,all,alerts/);
      expect(fake.sources[0].init).toEqual({ withCredentials: true });
      unsubscribe?.();
      hub.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports the transport mode once per page session", () => {
    const { hub } = hubOver("legacy", false);
    expect(hub.modeEventOnce()).toEqual({ key: "fallback", value: "secure=0,locks=0,bc=0" });
    expect(hub.modeEventOnce()).toBeNull();
  });

  it("reports the shared mode when the profile APIs are present", () => {
    const { hub } = hubOver("shared");
    expect(hub.modeEventOnce()).toEqual({ key: "shared", value: "secure=1,locks=1,bc=1" });
  });

  it("exposes the gateway origin the streams are opened against", () => {
    expect(hubOver("shared").hub.base).toBe("http://gateway.test");
  });
});

function plainTab(capability: "shared" | "legacy", secureContext = true): FakeTab {
  return new FakeTab({ network: new FakeNetwork(), capability, wallNow: () => 0, secureContext });
}

describe("shared stream support", () => {
  it("needs both tab messaging and locks", () => {
    expect(sharedSseSupported(plainTab("shared").transport)).toBe(true);
    expect(sharedSseSupported(plainTab("legacy").transport)).toBe(false);
    expect(sharedSseSupported({ openChannel: null, locks: plainTab("shared").transport.locks })).toBe(false);
    expect(sharedSseSupported({ openChannel: plainTab("shared").transport.openChannel, locks: null })).toBe(false);
  });

  it("reports the shared mode with the capability flags", () => {
    expect(transportModeReport(plainTab("shared").transport)).toEqual({
      key: "shared",
      value: "secure=1,locks=1,bc=1",
    });
  });

  it("reports the fallback mode and capability flags", () => {
    expect(transportModeReport(plainTab("legacy", false).transport)).toEqual({
      key: "fallback",
      value: "secure=0,locks=0,bc=0",
    });
  });
});
