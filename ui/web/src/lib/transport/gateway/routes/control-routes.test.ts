import { describe, expect, it } from "vitest";

import { configPath, harnessAvailabilityPath, inventoryPath, resolvedConfigPath, settingPath } from "./config-routes";
import { alertClassSamplesPath, alertsPath, fleetGraphPath, memoryNotePath, noticesPath } from "./control-routes";

describe("config routes", () => {
  it("getConfig appends ?machine= only when a machine is given", () => {
    expect(configPath()).toBe("/api/config");
    expect(configPath("runner-1")).toBe("/api/config?machine=runner-1");
  });

  it("getResolvedConfig appends ?model= only when a model is given", () => {
    expect(resolvedConfigPath("claude-opus-5")).toBe("/api/config/resolved?model=claude-opus-5");
    expect(resolvedConfigPath()).toBe("/api/config/resolved");
  });

  it("escapes names that are not path-safe", () => {
    expect(configPath("a b/c")).toBe("/api/config?machine=a%20b%2Fc");
    expect(inventoryPath("a&b")).toBe("/api/inventory?machine=a%26b");
    expect(settingPath("display.show agent")).toBe("/api/settings/display.show%20agent");
    expect(harnessAvailabilityPath("mac mini")).toBe("/api/cluster/machines/mac%20mini/harnesses");
  });
});

describe("control routes", () => {
  it("addresses one alert class by its full identity", () => {
    const path = alertClassSamplesPath(
      { level: "error", event_name: "turn failed", source: "runner", process: "" },
      24,
    );
    const url = new URL(path, "http://gateway");
    expect(url.pathname).toBe("/api/stats/alert-classes/samples");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      level: "error",
      event_name: "turn failed",
      source: "runner",
      process: "",
      hours: "24",
    });
  });

  it("alerts list only the filters that are set", () => {
    expect(alertsPath({})).toBe("/api/alerts");
    expect(alertsPath({ window: "24h", status: "unresolved", severity: "error", limit: 200 }))
      .toBe("/api/alerts?window=24h&status=unresolved&severity=error&limit=200");
  });

  it("notices list only the cursor fields that are set", () => {
    expect(noticesPath()).toBe("/api/notices");
    expect(noticesPath({ limit: 50, resolvedLimit: 10, beforeAt: "2026-10-01T00:00:00Z", beforeId: 7 }))
      .toBe("/api/notices?limit=50&resolved_limit=10&before_at=2026-10-01T00%3A00%3A00Z&before_id=7");
  });

  it("the fleet graph takes hours and the decay constant when given", () => {
    expect(fleetGraphPath()).toBe("/api/fleet/graph");
    expect(fleetGraphPath({ hours: 6, decayLambda: 0.5 })).toBe("/api/fleet/graph?hours=6&decay_lambda=0.5");
  });

  it("a memory note is addressed by its escaped path", () => {
    expect(memoryNotePath("notes/a b.md")).toBe("/api/memory/note?path=notes%2Fa+b.md");
  });
});
