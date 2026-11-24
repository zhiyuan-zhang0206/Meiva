// meiva:note
// The sidebar alerts bell: the firing count from the ["alerts", "firing"] cache, a new-tab link
// to Grafana's firing list, a grey "?" when Grafana cannot be reached, and nothing at all when
// nothing fires or no Grafana is configured.
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
  it("shows the firing count and opens Grafana's firing list in a new tab", () => {
    wrap(<AlertsBadge />, firing(5));
    const badge = screen.getByTestId("alerts-badge");
    expect(badge.getAttribute("href")).toBe("http://localhost:8000/grafana/alerting/list?search=state:firing");
    expect(badge.getAttribute("target")).toBe("_blank");
    expect(badge.getAttribute("rel")).toContain("noopener");
    expect(badge.getAttribute("aria-label")).toBe("5 firing alerts");
    expect(screen.getByTestId("alerts-badge-count").textContent).toBe("5");
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
