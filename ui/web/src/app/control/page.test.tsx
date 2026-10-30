// /control shell tests — the vertical layout: every section's heading + anchor
// renders at once (Ctrl-F / deep-link reachable), the sections compose in order,
// the left nav jumps to an anchor, and the observability sections (Status +
// Metrics) are NOT here — they moved to /insights. Section bodies are mocked to
// lightweight stubs so this test covers only the shell wiring (each body has its
// own test file); that also keeps the heavy per-section deps (api) out.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, within, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ControlShell } from "./page";
import { CONTROL_SCROLL_ID, CONTROL_SECTIONS } from "./sections";

// meiva:note
// The section bodies are stand-ins: the shell takes each one as a required prop, so this file
// covers only the shell wiring (each body has its own test file) and keeps the heavy per-section
// dependencies (api) out.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
function GuideBody() {
  return <div>GUIDE_BODY</div>;
}
function ConfigBody() {
  return <div>CONFIG_BODY</div>;
}
function DisplayBody() {
  return <div>DISPLAY_BODY</div>;
}
function PluginsBody() {
  return <div>PLUGINS_BODY</div>;
}
function McpBody() {
  return <div>MCP_BODY</div>;
}
function SkillsBody() {
  return <div>SKILLS_BODY</div>;
}
function SchedulesBody() {
  return <div>SCHEDULES_BODY</div>;
}

function ControlPage() {
  return (
    <ControlShell
      Guide={GuideBody}
      Config={ConfigBody}
      Display={DisplayBody}
      Plugins={PluginsBody}
      Mcp={McpBody}
      Skills={SkillsBody}
      Schedules={SchedulesBody}
    />
  );
}

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getUiContributions.mockResolvedValue({ themes: [], nav: [] });
});

afterEach(cleanup);

beforeEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return renderWithBrowser(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ControlPage shell", () => {
  it("renders a back-to-conversation link and the Control page title", () => {
    wrap(<ControlPage />);
    expect(screen.getByRole("heading", { name: "Control" })).toBeTruthy();
    expect(screen.getByRole("link", { name: /back to agents/i })).toBeTruthy();
  });

  it("uses the wider readable content column without forcing prose monospace", () => {
    wrap(<ControlPage />);
    const content = document.getElementById(CONTROL_SCROLL_ID)?.firstElementChild;

    expect(content?.classList.contains("max-w-6xl")).toBe(true);
    expect(content?.classList.contains("font-mono")).toBe(false);
  });

  it("renders every section heading + anchor at once (Ctrl-F / deep-link reachable)", () => {
    wrap(<ControlPage />);
    for (const section of CONTROL_SECTIONS) {
      // Heading always in the DOM, regardless of scroll position…
      expect(screen.getByRole("heading", { name: section.label })).toBeTruthy();
      // …and its anchor id resolves for URL hashes.
      expect(document.getElementById(section.id)).toBeTruthy();
    }
  });

  it("composes the section bodies in order", async () => {
    const { container } = wrap(<ControlPage />);
    await waitFor(() => expect(container.innerHTML).toContain("SCHEDULES_BODY"));
    const markers = [
      "GUIDE_BODY",
      "CONFIG_BODY",
      "DISPLAY_BODY",
      "PLUGINS_BODY",
      "MCP_BODY",
      "SKILLS_BODY",
      "SCHEDULES_BODY",
    ];
    const text = container.innerHTML;
    const positions = markers.map((m) => text.indexOf(m));
    expect(positions.every((p) => p >= 0)).toBe(true);
    // Strictly increasing → rendered top-to-bottom in the expected order.
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it("nav lists every section and jumps to its anchor on click", () => {
    const { browser } = wrap(<ControlPage />);
    const nav = screen.getByRole("navigation", { name: /control sections/i });
    // Every section is a nav entry (scope to the nav — labels also appear as
    // section headings on the page).
    for (const section of CONTROL_SECTIONS) {
      expect(within(nav).getAllByText(section.label).length).toBeGreaterThan(0);
    }
    fireEvent.click(within(nav).getByRole("button", { name: "Skills" }));
    expect(browser.actions.scrolled.map((scrolled) => (scrolled.target as HTMLElement).id)).toEqual([
      "skills",
    ]);
    expect(window.location.hash).toBe("#skills");
  });

  it("does NOT render the Status, Ops, Metrics or Permissions sections", () => {
    wrap(<ControlPage />);
    for (const label of ["Status", "Ops", "Metrics", "Permissions"]) {
      expect(screen.queryByRole("heading", { name: label })).toBeNull();
    }
    expect(document.getElementById("status")).toBeNull();
    expect(document.getElementById("ops")).toBeNull();
    expect(document.getElementById("metrics")).toBeNull();
    expect(document.getElementById("permissions")).toBeNull();
    const nav = screen.getByRole("navigation", { name: /control sections/i });
    for (const label of ["Status", "Ops", "Metrics", "Permissions"]) {
      expect(within(nav).queryByRole("button", { name: label })).toBeNull();
    }
  });
});

describe("Control shell — app settings section", () => {
  it("appears only inside the Meiva app", async () => {
    const first = wrap(<ControlPage />);
    expect(screen.queryByRole("heading", { name: "App" })).toBeNull();
    first.unmount();

    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {
      invoke: vi.fn().mockResolvedValue(undefined),
    };
    wrap(<ControlPage />);
    expect(await screen.findByRole("heading", { name: "App" })).toBeTruthy();
    delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });
});
