import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { renderWithBrowser } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
import { fakeBrowser } from "@/lib/browser/testing";
import { ControlShell } from "./page";

function Guide() { return <div>GUIDE_BODY</div>; }
function Config({ mode }: { mode: "general" | "advanced" | "machines" }) { return <div>{mode}_CONFIG</div>; }
function Display() { return <div>DISPLAY_BODY</div>; }
function Plugins() { return <div>PLUGINS_BODY</div>; }
function Mcp() { return <div>MCP_BODY</div>; }
function Skills() { return <div>SKILLS_BODY</div>; }
function Schedules() { return <div>SCHEDULES_BODY</div>; }

function renderSettings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithBrowser(<QueryClientProvider client={client}>
    <ControlShell Guide={Guide} Config={Config} Display={Display} Plugins={Plugins}
      Mcp={Mcp} Skills={Skills} Schedules={Schedules} />
  </QueryClientProvider>, { browser: fakeBrowser({ href: window.location.href, idle: "immediate" }) });
}

beforeEach(() => {
  resetTransport();
  transport.getUiContributions.mockResolvedValue({ themes: [], nav: [] });
  window.location.hash = "";
});
afterEach(cleanup);

describe("Settings navigation", () => {
  it("opens general settings and mounts only the selected panel", async () => {
    renderSettings();
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy();
    expect(await screen.findByText("general_CONFIG")).toBeTruthy();
    expect(screen.queryByText("SCHEDULES_BODY")).toBeNull();
    expect(screen.getByRole("link", { name: "Back to agents" }).getAttribute("href")).toBe("/");
    expect(screen.queryByText(/OKF/i)).toBeNull();
  });

  it("switches between machine configuration and automations with explicit scope", async () => {
    const { browser } = renderSettings();
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    fireEvent.click(within(nav).getByRole("button", { name: "Machines" }));
    expect(await screen.findByText("machines_CONFIG")).toBeTruthy();
    expect(screen.queryByText("general_CONFIG")).toBeNull();
    expect(browser.navigation.location()?.hash).toBe("#machines");
    fireEvent.click(within(nav).getByRole("button", { name: "Schedules" }));
    expect(await screen.findByText("SCHEDULES_BODY")).toBeTruthy();
    expect(within(nav).getByRole("button", { name: "Schedules" }).getAttribute("aria-current")).toBe("page");
  });

  it("maps a legacy technical deep link to advanced settings", async () => {
    window.location.hash = "#config-llm";
    renderSettings();
    expect(await screen.findByText("advanced_CONFIG")).toBeTruthy();
    expect(screen.queryByText("general_CONFIG")).toBeNull();
  });

  it("responds to hash navigation after mounting", async () => {
    const { browser } = renderSettings();
    browser.navigation.replace("#display-timeline", null);
    fireEvent(window, new Event("hashchange"));
    await waitFor(() => expect(screen.getByText("DISPLAY_BODY")).toBeTruthy());
  });
});
