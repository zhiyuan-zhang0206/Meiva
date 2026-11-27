// meiva:note
// The sidebar bell reads the firing cache, links to Grafana's active critical and error
// instances, and shows a grey "?" when Grafana cannot be reached.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AlertsBadge } from "@/components/notifications/alerts-badge";
import type { FiringAlerts } from "@/lib/contracts/types";
import { FIRING_ALERTS_QUERY_KEY } from "@/lib/notifications/use-firing-alerts";
import { apiWrapper, cacheOnlyApi } from "@/lib/transport/tests/api/api-provider";

afterEach(cleanup);

function firing(count: number): FiringAlerts {
  return { state: "ok", firing: count, critical: count, error: 0, warning: 0 };
}

const UNAVAILABLE: FiringAlerts = { state: "unavailable", firing: null, critical: null, error: null, warning: null };
const NOT_CONFIGURED: FiringAlerts = { state: "not_configured", firing: null, critical: null, error: null, warning: null };

function wrap(ui: React.ReactElement, seed: FiringAlerts) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  qc.setQueryData(FIRING_ALERTS_QUERY_KEY, seed);
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>, {
    wrapper: apiWrapper(cacheOnlyApi()),
  });
}

describe("AlertsBadge", () => {
  it("shows the critical and error count and links to those instances in a new tab", () => {
    wrap(<AlertsBadge />, { state: "ok", firing: 8, critical: 1, error: 2, warning: 5 });
    const badge = screen.getByTestId("alerts-badge");
    expect(badge.getAttribute("href")).toBe("http://localhost:8000/grafana/alerting/groups?alertState=active&queryString=severity%3D~%22critical%7Cerror%22&groupBy=alertname%2Cmachine");
    expect(badge.getAttribute("target")).toBe("_blank");
    expect(badge.getAttribute("rel")).toContain("noopener");
    expect(badge.getAttribute("aria-label")).toBe("3 active critical or error alerts");
    expect(badge.getAttribute("title")).toBe("3 active critical or error alerts");
    const pill = screen.getByTestId("alerts-badge-count");
    expect(pill.textContent).toBe("3");
    expect(pill.className).toContain("bg-destructive");
  });

  it("renders no badge when only warnings fire", () => {
    wrap(<AlertsBadge />, { state: "ok", firing: 3, critical: 0, error: 0, warning: 3 });
    expect(screen.queryByTestId("alerts-badge")).toBeNull();
  });

  it("renders nothing when nothing fires", () => {
    wrap(<AlertsBadge />, firing(0));
    expect(screen.queryByTestId("alerts-badge")).toBeNull();
  });

  it("renders nothing when no Grafana is configured", () => {
    wrap(<AlertsBadge />, NOT_CONFIGURED);
    expect(screen.queryByTestId("alerts-badge")).toBeNull();
  });

  it("an unreachable Grafana is a grey ?, not a zero and not a count", () => {
    wrap(<AlertsBadge />, UNAVAILABLE);
    const pill = screen.getByTestId("alerts-badge-count");
    expect(pill.textContent).toBe("?");
    expect(pill.className).toContain("bg-muted");
    expect(pill.className).not.toContain("bg-destructive");
    expect(screen.getByTestId("alerts-badge").getAttribute("aria-label")).toBe(
      "Grafana unreachable, alert status unknown",
    );
  });

  it("an ok answer without a count is unknown, never zero", () => {
    wrap(<AlertsBadge />, { state: "ok", firing: null, critical: null, error: null, warning: null });
    expect(screen.getByTestId("alerts-badge-count").textContent).toBe("?");
  });

  it("matches the sidebar icon row's hit box rather than a header pill", () => {
    wrap(<AlertsBadge />, firing(3));
    const badge = screen.getByTestId("alerts-badge");
    expect(badge.className).toContain("p-1.5");
    expect(badge.className).toContain("hover:bg-sidebar-accent");
  });

  it("caps the badge at 99+", () => {
    wrap(<AlertsBadge />, firing(250));
    expect(screen.getByTestId("alerts-badge-count").textContent).toBe("99+");
  });
});
