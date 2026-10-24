import { describe, expect, it } from "vitest";

import { slowRequestReport } from "./timing-plan";

const GATEWAY_TELEMETRY_KEY_PATTERN = /^[a-z0-9._-]{1,128}$/;

describe("slow request report", () => {
  it("reports a slow request with query strings removed and numeric segments collapsed", () => {
    expect(slowRequestReport("/api/agents/2697/inspect/statistics?hours=1", 801)).toEqual({
      key: "agents.id.inspect.statistics",
      value: 801,
      dedupe: false,
    });
  });

  it("emits gateway-valid keys for representative API routes", () => {
    const routes = [
      ["/api/status", "status"],
      ["/api/stats/dashboard?hours=6", "stats.dashboard"],
      ["/api/agents/2697/inspect/statistics?hours=1", "agents.id.inspect.statistics"],
      ["/api/agents/2697/shell/42", "agents.id.shell.id"],
      ["/api/fleet/graph", "fleet.graph"],
      ["/api/memory/search?q=x", "memory.search"],
    ] as const;
    const keys = routes.map(([path]) => slowRequestReport(path, 801)?.key);
    expect(keys).toEqual(routes.map(([, expected]) => expected));
    for (const key of keys) expect(key).toMatch(GATEWAY_TELEMETRY_KEY_PATTERN);
  });

  it("does not report a request at the 800 ms threshold", () => {
    expect(slowRequestReport("/api/status", 800)).toBeNull();
    expect(slowRequestReport("/api/status", 12)).toBeNull();
  });

  it("does not time the frontend telemetry endpoint", () => {
    expect(slowRequestReport("/api/frontend-telemetry", 2_000)).toBeNull();
    expect(slowRequestReport("/api/frontend-telemetry?x=1", 2_000)).toBeNull();
  });

  it("rounds the duration to whole milliseconds", () => {
    expect(slowRequestReport("/api/status", 900.4)?.value).toBe(900);
    expect(slowRequestReport("/api/status", 900.6)?.value).toBe(901);
  });
});
