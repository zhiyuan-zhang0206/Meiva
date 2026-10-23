"use client";

// meiva:note
// The footer's icon row is ordered by standing state first, navigation second: the
// unresolved-alert bell leads it, ahead of Fleet (user 2026-10-21), and renders only
// while the count is non-zero.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render as rtlRender, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ALERTS_QUERY_KEY } from "@/lib/notifications/use-alerts";
import { api } from "@/lib/transport/api";

import { SidebarFooter } from "./footer";

const clients = new Set<QueryClient>();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/transport/api", () => ({
  api: {
    getAlerts: vi.fn(() => new Promise(() => undefined)),
    getStatsDashboard: () => new Promise(() => undefined),
    getUiContributions: () => Promise.resolve({ themes: [], nav: [], stats: [] }),
  },
}));

function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.add(qc);
  qc.setQueryData(ALERTS_QUERY_KEY, { alerts: [], meta: { window: "24h", total: 0, unresolved_count: 3 } });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
  vi.clearAllMocks();
});

describe("SidebarFooter icon row", () => {
  it("leads the row with the unresolved-alert bell", () => {
    render(<SidebarFooter activeAgentId={7} />);
    const bell = screen.getByTestId("alerts-badge");
    expect(bell.parentElement?.firstElementChild).toBe(bell);
  });

  it("puts Fleet, Insights and Control after the bell", () => {
    render(<SidebarFooter activeAgentId={7} />);
    const row = screen.getByTestId("alerts-badge").parentElement!;
    const labels = [...row.querySelectorAll("[aria-label]")].map((node) => node.getAttribute("aria-label"));
    expect(labels.slice(0, 4)).toEqual(["3 unresolved alerts", "Fleet", "Insights", "Control"]);
    expect(api.getAlerts).not.toHaveBeenCalled();
  });
});
