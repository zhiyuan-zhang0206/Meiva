// meiva:note
// Plugins use one cluster endpoint; MCP retains per-host capability and installation coverage.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { fakeNavigation, navigationWrapper } from "@/lib/browser/navigation/testing";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { InventoryAggregate, InventoryWriteResult } from "@/lib/contracts/types";

import InventoryPage from "./page";
import { renderWithApi as render } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});


// meiva:note
// The page navigates through the app's navigation port, not Next's router: the moves it asked for
// are read off the fake that stands in for the port.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
const navigation = fakeNavigation();

afterEach(cleanup);
// meiva:note
// Each test answers the fake's endpoints; reset between tests so one case's calls and
// answers do not leak into the next.
beforeEach(() => {
  vi.restoreAllMocks();
  resetTransport();
  navigation.moves.length = 0;
  transport.getPlugins.mockResolvedValue({ plugins: [
    { name: "meiva_code", enabled: true, user_toggleable: true, category: "builtin", description: "Code execution plugin" },
    { name: "only_h1", enabled: false, user_toggleable: true, category: "extension", description: "Only on h1" },
  ] });
});

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrap(ui: React.ReactElement, qc?: QueryClient) {
  const client = qc ?? makeQc();
  // meiva:note
  // The page carries timeline cards, which age their stamps on the page's wall clock: the provider
  // the app mounts is what `useWallNow` reads, and without it the render throws.
  // meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
  const Page = navigationWrapper(navigation);
  return render(
    <WallClockProvider wallNow={() => Date.now()}>
      <Page>
        <QueryClientProvider client={client}>{ui}</QueryClientProvider>
      </Page>
    </WallClockProvider>,
  );
}

const H1 = "h1";
const H2 = "h2";

const AGGREGATE: InventoryAggregate = {
  machines: [H1, H2],
  unreachable: [],
  plugins: [
    {
      name: "meiva_code",
      kind: "plugin",
      description: "Code execution plugin",
      hosts: {
        [H1]: { present: true, enabled: true, can_enable: null, reason: null },
        [H2]: { present: true, enabled: false, can_enable: null, reason: null },
      },
    },
    {
      name: "only_h1",
      kind: "plugin",
      description: "Only on h1",
      hosts: {
        [H1]: { present: true, enabled: false, can_enable: null, reason: null },
        [H2]: { present: false, enabled: false, can_enable: null, reason: null },
      },
    },
  ],
  mcp_servers: [
    {
      name: "needs_display",
      kind: "mcp",
      description: "unix:///private/preview/chrome-mcp.sock",
      hosts: {
        [H1]: {
          present: true,
          enabled: false,
          can_enable: false,
          reason: "requires a display",
        },
        [H2]: { present: false, enabled: false, can_enable: null, reason: null },
      },
    },
  ],
};

const OK_RESULT: InventoryWriteResult = {
  applied: true,
  plugin_results: {},
  mcp_results: {},
};

describe("InventoryPage tri-state", () => {
  it("loading shows a spinner placeholder", () => {
    transport.getInventory.mockReturnValue(
      new Promise(() => undefined /* never resolves */),
    );
    wrap(<InventoryPage />);
    expect(screen.getByLabelText("Loading MCP servers…")).toBeTruthy();
  });

  it("error shows a quiet line, not the raw error", async () => {
    transport.getInventory.mockRejectedValue(new Error("network down"));
    wrap(<InventoryPage />);
    await waitFor(() => expect(screen.getAllByText(/Couldn't load inventory/).length).toBeGreaterThan(0));
    expect(screen.queryByText(/network down/)).toBeNull();
  });
});

describe("InventoryPage render", () => {
  it("renders both sections and the selected machine MCP list", async () => {
    transport.getInventory.mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    await waitFor(() => screen.getByText("meiva_code"));
    expect(screen.getByText("Plugins")).toBeTruthy();
    expect(screen.getByText("MCP servers")).toBeTruthy();
    expect(screen.getByText("only_h1")).toBeTruthy();
    expect(screen.getByText("needs_display")).toBeTruthy();
    expect(screen.queryByText("unix:///private/preview/chrome-mcp.sock")).toBeNull();
    expect(screen.getByText("Code execution plugin")).toBeTruthy();
  });

  it("selects one machine and lists only its installed MCP servers", async () => {
    transport.getInventory.mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    const selector = await screen.findByRole("combobox", { name: "Machine" });
    expect(screen.getByText("needs_display")).toBeTruthy();
    fireEvent.change(selector, { target: { value: H2 } });
    expect(screen.queryByText("needs_display")).toBeNull();
    expect(screen.getByText("No MCP servers installed on this machine.")).toBeTruthy();
    expect(screen.queryByRole("columnheader")).toBeNull();
  });

  it("keeps unreachable machines selectable and reports unknown state", async () => {
    transport.getInventory.mockResolvedValue({ ...AGGREGATE, unreachable: [H2] });
    wrap(<InventoryPage />);
    fireEvent.change(await screen.findByRole("combobox", { name: "Machine" }), { target: { value: H2 } });
    expect(screen.getByRole("status").textContent).toContain("Its MCP installation and enablement state are unknown");
    expect(screen.queryByText("No MCP servers installed on this machine.")).toBeNull();
    expect(screen.queryByText("needs_display")).toBeNull();
  });

  it("can't-enable cell is disabled and shows its reason as title", async () => {
    transport.getInventory.mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    const cell = await waitFor(() =>
      screen.getByRole<HTMLButtonElement>("switch", { name: "requires a display" }),
    );
    expect(cell.disabled).toBe(true);
    expect(cell.getAttribute("aria-label")).toMatch(/requires a display/);
  });
});

describe("InventoryPage toggle (putInventory)", () => {


  it("an mcp cell builds an mcp_servers body, not a plugins body", async () => {
    // Use an aggregate where the mcp server is enabled on h1 so its cell is
    // clickable (turning it OFF works even when can_enable is false).
    const agg: InventoryAggregate = {
      ...AGGREGATE,
      mcp_servers: [
        {
          name: "needs_display",
          kind: "mcp",
          description: "unix:///private/preview/chrome-mcp.sock",
          hosts: {
            [H1]: { present: true, enabled: true, can_enable: false, reason: "requires a display" },
            [H2]: { present: false, enabled: false, can_enable: null, reason: null },
          },
        },
      ],
    };
    transport.getInventory.mockResolvedValue(agg);
    const putSpy = transport.putInventory.mockResolvedValue(OK_RESULT);
    wrap(<InventoryPage />);
    const cell = await waitFor(() =>
      screen.getByRole<HTMLButtonElement>("switch", { name: "Toggle needs_display on h1" }),
    );
    // Enabled mcp item is clickable even with can_enable:false.
    expect(cell.disabled).toBe(false);
    fireEvent.click(cell);
    await waitFor(() =>
      expect(putSpy).toHaveBeenCalledWith({ mcp_servers: { needs_display: false } }, H1),
    );
  });


});

// Both sections render on this page, so these also pin that each entry carries
// its OWN kind — the bug a shared component invites.
describe("InventoryPage install entries", () => {
  it("the plugins entry posts kind=plugin and opens the installer's conversation", async () => {
    transport.getInventory.mockResolvedValue(AGGREGATE);
    const draft = transport.draftPackage.mockResolvedValue({ agent_id: 61 });
    wrap(<InventoryPage />);

    fireEvent.change(await screen.findByPlaceholderText(/Describe a plugin you want/), {
      target: { value: "review my PRs" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find and install a plugin" }));

    await waitFor(() => expect(draft).toHaveBeenCalledWith("plugin", "review my PRs"));
    await waitFor(() => expect(navigation.moves).toContainEqual({ kind: "push", href: "/" }));
  });

  it("the MCP entry posts kind=mcp", async () => {
    transport.getInventory.mockResolvedValue(AGGREGATE);
    const draft = transport.draftPackage.mockResolvedValue({ agent_id: 62 });
    wrap(<InventoryPage />);

    fireEvent.change(await screen.findByPlaceholderText(/Describe a tool you want to reach/), {
      target: { value: "query our Linear issues" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find and install an MCP server" }));

    await waitFor(() => expect(draft).toHaveBeenCalledWith("mcp", "query our Linear issues"));
  });

  it("both entries survive a failed inventory read", async () => {
    transport.getInventory.mockRejectedValue(new Error("network down"));
    wrap(<InventoryPage />);
    await waitFor(() =>
      expect(screen.getAllByText(/Couldn't load inventory/).length).toBeGreaterThan(0),
    );
    expect(screen.getByRole("button", { name: "Find and install a plugin" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Find and install an MCP server" })).toBeTruthy();
  });
});


it("writes only the selected machine and surfaces the host rejection", async () => {
  transport.getInventory.mockResolvedValue({ ...AGGREGATE, mcp_servers: [{
    ...AGGREGATE.mcp_servers[0],
    hosts: {
      [H1]: { present: true, enabled: true, can_enable: true, reason: null },
      [H2]: { present: true, enabled: false, can_enable: true, reason: null },
    },
  }] });
  transport.putInventory.mockResolvedValue({ applied: false, plugin_results: {}, mcp_results: { needs_display: { ok: false, reason: "credentials unavailable" } } });
  wrap(<InventoryPage />);
  fireEvent.change(await screen.findByRole("combobox", { name: "Machine" }), { target: { value: H2 } });
  fireEvent.click(screen.getByRole("switch", { name: "Toggle needs_display on h2" }));
  await waitFor(() => expect(transport.putInventory.mock.calls).toEqual([[{ mcp_servers: { needs_display: true } }, H2]]));
  expect(await screen.findByText(/Change not applied: credentials unavailable/)).toBeTruthy();
  expect(screen.getByRole("switch", { name: "Toggle needs_display on h2" }).getAttribute("aria-checked")).toBe("false");
});

it("keeps the selected machine when a write fails and can switch machines afterwards", async () => {
  transport.getInventory.mockResolvedValue({ ...AGGREGATE, mcp_servers: [{
    ...AGGREGATE.mcp_servers[0],
    hosts: { [H1]: { present: true, enabled: true, can_enable: true, reason: null } },
  }] });
  transport.putInventory.mockRejectedValue(new Error("host unavailable"));
  wrap(<InventoryPage />);
  fireEvent.click(await screen.findByRole("switch", { name: "Toggle needs_display on h1" }));
  expect(await screen.findByText(/Save failed: host unavailable/)).toBeTruthy();
  fireEvent.change(screen.getByRole("combobox", { name: "Machine" }), { target: { value: H2 } });
  expect(screen.queryByText(/Save failed: host unavailable/)).toBeNull();
  expect(screen.getByText("No MCP servers installed on this machine.")).toBeTruthy();
});
