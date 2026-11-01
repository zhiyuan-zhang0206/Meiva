"use client";

// meiva:note
// The footer's icon row is ordered by standing state first, navigation second: the
// unresolved-alert bell leads it, ahead of Fleet (user 2026-10-21), and renders only
// while the count is non-zero.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";

import { fakeNavigation, navigationWrapper } from "@/lib/browser/navigation/testing";
import { ALERTS_QUERY_KEY } from "@/lib/notifications/use-alerts";
import { ICON_SIZE_CLASS } from "@/lib/layout/layout";

import { SidebarFooter } from "./footer";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { renderWithBrowser as rtlRender } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getAlerts.mockImplementation(() => new Promise(() => undefined));
  transport.getStatsDashboard.mockImplementation(() => new Promise(() => undefined));
  transport.getUiContributions.mockImplementation(() => Promise.resolve({ themes: [], nav: [], stats: [] }));
});


const clients = new Set<QueryClient>();

// meiva:note
// The footer's jumps go to the navigation value the harness hands in; no module is replaced.
const navigation = fakeNavigation();

// meiva:note
// The footer's status cards age on the page's clock; the real one keeps this file's strings as they
// were, since no case asserts a particular age.
function renderFooter(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.add(qc);
  qc.setQueryData(ALERTS_QUERY_KEY, { alerts: [], meta: { window: "24h", total: 0, unresolved_count: 3 } });
  return rtlRender(
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </WallClockProvider>,
    { wrapper: navigationWrapper(navigation) },
  );
}

afterEach(() => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
  vi.clearAllMocks();
});

describe("SidebarFooter icon row", () => {
  it("leads the row with the unresolved-alert bell", () => {
    renderFooter(<SidebarFooter activeAgentId={7} />);
    const bell = screen.getByTestId("alerts-badge");
    expect(bell.parentElement?.firstElementChild).toBe(bell);
  });

  // meiva:note
  // The footer's rule is its own and never aligned with the composer's (user
  // ruling 2026-11-01): it neither tracks the composer's measured height (user
  // report 2026-10-29 — a divider that followed the composer's line read as its
  // own apparatus) nor borrows the composer's band (its `pt-3 pb-6` padding and
  // `min-h-9` control row), which would make the footer's rule derive from the composer's.
  // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/bottom-rules.meiva.okf.md
  it("keeps its own band height instead of tracking the composer", () => {
    renderFooter(<SidebarFooter activeAgentId={7} />);
    const footer = screen.getByTestId("sidebar-footer");
    expect(footer.style.minHeight).toBe("");
    expect(footer.style.getPropertyValue("--composer-band-height")).toBe("");
  });

  it("draws the sidebar's own compact band, not the composer's", () => {
    renderFooter(<SidebarFooter activeAgentId={7} />);
    const footer = screen.getByTestId("sidebar-footer");
    const classes = footer.className.split(" ");
    for (const cls of ["border-t", "py-1"]) expect(classes).toContain(cls);
    for (const cls of ["pt-2", "pb-3", "pt-3", "pb-6"]) expect(classes).not.toContain(cls);
    const row = footer.firstElementChild!;
    expect(row.className.split(" ")).not.toContain("min-h-9");
  });

  it("puts every row glyph on the one shell icon size", () => {
    // meiva:note
    // User ruling 2026-10-29: the composer's upload glyph (16px) is the
    // reference for the shell's icon buttons.
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    renderFooter(<SidebarFooter activeAgentId={7} />);
    const icons = screen.getByTestId("sidebar-footer").querySelectorAll("svg");
    expect(icons.length).toBeGreaterThan(0);
    for (const icon of icons) expect(icon.getAttribute("class")).toContain(ICON_SIZE_CLASS);
  });

  it("puts Fleet, Insights and Control after the bell", () => {
    renderFooter(<SidebarFooter activeAgentId={7} />);
    const row = screen.getByTestId("alerts-badge").parentElement!;
    const labels = [...row.querySelectorAll("[aria-label]")].map((node) => node.getAttribute("aria-label"));
    expect(labels.slice(0, 4)).toEqual(["3 unresolved alerts", "Fleet", "Insights", "Control"]);
  });
});
