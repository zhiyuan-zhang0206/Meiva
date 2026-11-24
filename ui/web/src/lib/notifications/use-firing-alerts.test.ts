import { describe, expect, it } from "vitest";

import type { FiringAlerts } from "@/lib/contracts/types";

import { alertsSignal } from "./use-firing-alerts";

const NONE = { firing: null, critical: null, error: null, warning: null };

describe("alertsSignal", () => {
  it("is a count when Grafana reports alerts firing", () => {
    const ok: FiringAlerts = { state: "ok", firing: 3, critical: 1, error: 1, warning: 1 };
    expect(alertsSignal(ok)).toEqual({ kind: "count", count: 3 });
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
