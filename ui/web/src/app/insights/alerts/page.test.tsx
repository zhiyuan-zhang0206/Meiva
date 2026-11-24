import { describe, expect, it, vi } from "vitest";

import { AlertsRedirectView } from "./page";

const redirect = vi.fn();

describe("AlertsRedirect", () => {
  it("forwards the old alert deep link to the Insights Ops section, where the Grafana alerts link is", () => {
    AlertsRedirectView({ redirect });

    expect(redirect).toHaveBeenCalledWith("/insights#ops");
  });
});
