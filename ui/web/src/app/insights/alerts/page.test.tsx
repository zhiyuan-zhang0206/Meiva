import { describe, expect, it, vi } from "vitest";

import { AlertsRedirectView } from "./page";

const redirect = vi.fn();

describe("AlertsRedirect", () => {
  it("forwards the alert-history deep link to the Insights anchor", () => {
    AlertsRedirectView({ redirect });

    expect(redirect).toHaveBeenCalledWith("/insights#alerts");
  });
});
