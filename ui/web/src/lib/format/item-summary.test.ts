import { describe, expect, it } from "vitest";

import { formatDuration, orderSdkCalls, sdkNamespace, summarizeCode } from "./item-summary";

describe("summarizeCode", () => {
  it("renders recorded calls by namespace, then count and method name", () => {
    const s = summarizeCode([
      { method: "shell.run", count: 5 },
      { method: "files.read", count: 2 },
      { method: "files.write", count: 1 },
    ]);
    expect(s.calls).toEqual([
      { method: "files.read", count: 2 },
      { method: "files.write", count: 1 },
      { method: "shell.run", count: 5 },
    ]);
    expect(s.totalCalls).toBe(8);
  });

  it("trusts an authoritative empty array", () => {
    expect(summarizeCode([])).toEqual({
      calls: [],
      totalCalls: 0,
    });
  });

  it.each([undefined, null])(
    "absent sdk_calls (%s) shows no calls",
    (sdkCalls) => {
      expect(summarizeCode(sdkCalls)).toEqual({
        calls: [],
        totalCalls: 0,
      });
    },
  );
});

describe("orderSdkCalls", () => {
  it("groups interleaved namespaces alphabetically even when counts tie", () => {
    expect(orderSdkCalls([
      { method: "shell.run", count: 3 },
      { method: "files.write", count: 2 },
      { method: "agents.spawn", count: 3 },
      { method: "files.read", count: 3 },
      { method: "shell.sessions.list", count: 2 },
    ])).toEqual([
      { method: "agents.spawn", count: 3 },
      { method: "files.read", count: 3 },
      { method: "files.write", count: 2 },
      { method: "shell.run", count: 3 },
      { method: "shell.sessions.list", count: 2 },
    ]);
    expect(sdkNamespace("shell.sessions.list")).toBe("shell");
  });

  it("returns an empty list for no calls", () => {
    expect(orderSdkCalls([])).toEqual([]);
  });

  it("keeps a single call", () => {
    expect(orderSdkCalls([{ method: "files.read", count: 1 }])).toEqual([
      { method: "files.read", count: 1 },
    ]);
  });

  it("uses the whole method as the namespace when there is no dot", () => {
    expect(sdkNamespace("status")).toBe("status");
    expect(orderSdkCalls([
      { method: "status", count: 2 },
      { method: "shell.run", count: 1 },
    ])).toEqual([
      { method: "shell.run", count: 1 },
      { method: "status", count: 2 },
    ]);
  });

  it("keeps namespace order independent of counts", () => {
    expect(orderSdkCalls([
      { method: "shell.run", count: 100 },
      { method: "agents.spawn", count: 1 },
    ])).toEqual([
      { method: "agents.spawn", count: 1 },
      { method: "shell.run", count: 100 },
    ]);
  });
});

describe("formatDuration", () => {
  it("sub-0.1s floors to 0.1s", () => {
    expect(formatDuration(0)).toBe("0.1s");
    expect(formatDuration(50)).toBe("0.1s");
    expect(formatDuration(99)).toBe("0.1s");
  });

  it("0.1s-1s rounds to 0.1s", () => {
    expect(formatDuration(150)).toBe("0.2s");
    expect(formatDuration(340)).toBe("0.3s");
    expect(formatDuration(949)).toBe("0.9s");
    expect(formatDuration(950)).toBe("1s");
  });

  it("1s-60s rounds to whole seconds", () => {
    expect(formatDuration(1_200)).toBe("1s");
    expect(formatDuration(8_400)).toBe("8s");
    expect(formatDuration(45_600)).toBe("46s");
  });

  it("over a minute switches to m/s", () => {
    expect(formatDuration(72_000)).toBe("1m 12s");
    expect(formatDuration(120_000)).toBe("2m");
  });
});
