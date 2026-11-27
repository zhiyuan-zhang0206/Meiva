import { describe, expect, it } from "vitest";

import type { FiringAlerts } from "@/lib/contracts/types";

import { alertsSignal } from "./use-firing-alerts";

const NONE = { firing: null, critical: null, error: null, warning: null };

describe("alertsSignal", () => {
  it("counts only firing critical and error alerts", () => {
    const ok: FiringAlerts = { state: "ok", firing: 8, critical: 1, error: 2, warning: 5 };
    expect(alertsSignal(ok)).toEqual({ kind: "count", count: 3 });
  });

  it("is nothing when only warnings fire", () => {
    expect(alertsSignal({ state: "ok", firing: 3, critical: 0, error: 0, warning: 3 })).toBeNull();
  });

  it("is nothing when nothing fires", () => {
    expect(alertsSignal({ state: "ok", firing: 0, critical: 0, error: 0, warning: 0 })).toBeNull();
  });

  it("is nothing before the first answer and when no Grafana is configured", () => {
    expect(alertsSignal(undefined)).toBeNull();
    expect(alertsSignal({ state: "not_configured", ...NONE })).toBeNull();
  });

  it("is unknown, never zero, when Grafana did not answer", () => {
    expect(alertsSignal({ state: "unavailable", ...NONE })).toEqual({ kind: "unknown" });
    expect(alertsSignal({ state: "ok", ...NONE })).toEqual({ kind: "unknown" });
  });
});
