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
    { name: "meiva_code", enabled: true, description: "Code execution plugin" },
    { name: "only_h1", enabled: false, description: "Only on h1" },
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

// meiva_code: present+enabled on h1, present+disabled on h2 -> 1/2 enabled.
// only_h1: present on h1 only -> its h2 cell is "not installed".
// mcp "needs_display": present+disabled on h1 with can_enable:false.
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
    // The spinner is an svg with the animate-spin class.
    expect(document.querySelector(".animate-spin")).toBeTruthy();
  });

  it("error shows a quiet line, not the raw error", async () => {
    transport.getInventory.mockRejectedValue(new Error("network down"));
    wrap(<InventoryPage />);
    // Both the Plugins and MCP matrices surface the same quiet line (one query,
    // two sections), so assert presence via getAllByText.
    await waitFor(() => expect(screen.getAllByText(/Couldn't load inventory/).length).toBeGreaterThan(0));
    expect(screen.queryByText(/network down/)).toBeNull();
  });
});

describe("InventoryPage render", () => {
  it("renders both section headers + every item row", async () => {
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

  it("machine columns appear as headers", async () => {
    transport.getInventory.mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    await waitFor(() => screen.getByText("meiva_code"));
    // Each machine appears as a header cell.
    expect(screen.getAllByText(H1).length).toBeGreaterThan(0);
    expect(screen.getAllByText(H2).length).toBeGreaterThan(0);
  });


  it("not-installed cell shows — for a host lacking the item", async () => {
    transport.getInventory.mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    await waitFor(() => screen.getByText("only_h1"));
    // only_h1 has no h1/h2 toggle for the h2 column (not installed there).
    expect(screen.queryByTestId(`cell-only_h1-${H2}`)).toBeNull();
    // A "—" placeholder is rendered for the not-installed cell.
    expect(screen.getAllByLabelText("Not installed on this host").length).toBeGreaterThan(0);
  });

  it("can't-enable cell is disabled and shows its reason as title", async () => {
    transport.getInventory.mockResolvedValue(AGGREGATE);
    wrap(<InventoryPage />);
    const cell = await waitFor(() =>
      screen.getByTestId<HTMLButtonElement>(`cell-needs_display-${H1}`),
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
      screen.getByTestId<HTMLButtonElement>(`cell-needs_display-${H1}`),
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
