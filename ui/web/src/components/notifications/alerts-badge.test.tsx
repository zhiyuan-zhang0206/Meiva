// meiva:note
// The sidebar alerts badge: the unresolved count from the ["alerts"] cache, rendered
// beside the Settings shortcut, and nothing at all at zero.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AlertsBadge } from "@/components/notifications/alerts-badge";
import type { AlertsResponse } from "@/lib/contracts/types";

afterEach(cleanup);

function response(unresolved: number): AlertsResponse {
  return {
    alerts: [],
    meta: { window: "24h", total: 0, unresolved_count: unresolved },
  };
}

function wrap(ui: React.ReactElement, seed: AlertsResponse | null) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  if (seed) qc.setQueryData(["alerts"], seed);
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("AlertsBadge", () => {
  it("shows the unresolved count and links to the alert section", () => {
    wrap(<AlertsBadge />, response(5));
    const badge = screen.getByTestId("alerts-badge");
    expect(badge.getAttribute("href")).toBe("/insights#alerts");
    expect(badge.getAttribute("aria-label")).toBe("5 unresolved alerts");
    expect(screen.getByTestId("alerts-badge-count").textContent).toBe("5");
  });

  it("renders nothing at zero unresolved", () => {
    wrap(<AlertsBadge />, response(0));
    expect(screen.queryByTestId("alerts-badge")).toBeNull();
  });

  it("matches the sidebar icon row's hit box rather than a header pill", () => {
    wrap(<AlertsBadge />, response(3));
    const badge = screen.getByTestId("alerts-badge");
    expect(badge.className).toContain("p-1.5");
    expect(badge.className).toContain("hover:bg-sidebar-accent");
  });

  it("caps the badge at 99+", () => {
    wrap(<AlertsBadge />, response(250));
    expect(screen.getByTestId("alerts-badge-count").textContent).toBe("99+");
  });
});
