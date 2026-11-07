import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { renderWithBrowser } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
import { fakeBrowser } from "@/lib/browser/testing";
import { ControlShell } from "./page";

function Guide() { return <div>GUIDE_BODY</div>; }
function Config({ mode }: { mode: "general" | "advanced" | "machines" }) { return <div>{mode}_CONFIG</div>; }
function Display({ mode }: { mode?: "general" | "advanced" | "all" }) { return <div>{mode}_DISPLAY</div>; }
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
    expect(screen.getByText("general_DISPLAY")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to agents" }).getAttribute("href")).toBe("/");
    expect(screen.queryByText(/OKF/i)).toBeNull();
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(nav).getAllByRole("button")[0].textContent).toBe("Guide");
  });

  it("places machine resources inside Advanced and keeps automations separate", async () => {
    const { browser } = renderSettings();
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(within(nav).queryByRole("button", { name: "Machines" })).toBeNull();
    expect(within(nav).queryByRole("button", { name: "Display" })).toBeNull();
    fireEvent.click(within(nav).getByRole("button", { name: "Advanced" }));
    fireEvent.click(screen.getByRole("button", { name: "Resources & services" }));
    expect(await screen.findByText("machines_CONFIG")).toBeTruthy();
    expect(screen.queryByText("general_CONFIG")).toBeNull();
    expect(browser.navigation.location()?.hash).toBe("#advanced-resources");
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


  it("preserves the old machine link inside Advanced", async () => {
    window.location.hash = "#machines";
    renderSettings();
    expect(await screen.findByText("machines_CONFIG")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Advanced" }).getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("button", { name: "Resources & services" }).getAttribute("aria-current")).toBe("page");
  });

  it("responds to hash navigation after mounting", async () => {
    const { browser } = renderSettings();
    browser.navigation.replace("#display-timeline", null);
    fireEvent(window, new Event("hashchange"));
    await waitFor(() => expect(screen.getByText("advanced_DISPLAY")).toBeTruthy());
  });
});
