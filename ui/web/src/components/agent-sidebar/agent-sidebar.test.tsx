import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BAR_DIVIDER_CLASS } from "@/lib/layout/layout";
import type * as SidebarModule from "@/lib/agents/sidebar";
import type { PluginStat } from "@/lib/contracts/types";
import type * as PluginStatsModule from "@/lib/plugins/plugin-stats";
import {
  bpMock,
  handlers,
  makeAgent,
  pushSpy,
  resetSidebarTestState,
  sortStore,
  state,
  StubAgentRow,
  StubButton,
  StubScrollArea,
  StubSpawnForm,
  wrap,
} from "./sidebar-test-harness";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushSpy }),
}));

vi.mock("@/lib/state/store", () => ({
  useStore: <T,>(selector: (s: unknown) => T): T => {
    const fake = {
      activeId: state.activeId,
      setActiveId: state.setActiveId,
      mobileSidebarOpen: state.mobileSidebarOpen,
      setMobileSidebarOpen: state.setMobileSidebarOpen,
      searchQuery: state.searchQuery,
      setSearchQuery: state.setSearchQuery,
    };
    return selector(fake);
  },
}));

vi.mock("@/lib/agents/sidebar", async () => {
  const actual = await vi.importActual<typeof SidebarModule>("@/lib/agents/sidebar");
  const React = await import("react");
  return {
    ...actual,
    useSidebarCollapsed: () => ({
      collapsed: state.sidebarCollapsed,
      setCollapsed: state.setSidebarCollapsed,
    }),
    useStatsDashboard: () => ({
      stats: state.stats,
      error: state.statsError,
      isFetching: state.statsFetching,
      refetch: state.statsRefetch,
    }),
    useStatsWindow: () => ({
      windowHours: state.statsWindowHours,
      setWindowHours: state.setStatsWindowHours,
    }),
    useSidebarViewMode: () => ({
      viewMode: state.sidebarViewMode,
      setViewMode: state.setSidebarViewMode,
    }),
    // meiva:note
    // Sort is DB-backed (its persistence is covered in lib/sidebar.test.ts);
    // here only the reorder-on-click UI matters. The stub is shared across
    // consumers because the list and the settings popover read one store in the
    // app: a per-hook useState gave each its own copy, so a key clicked in the
    // popover never reordered the list.
    // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
    useSidebarSort: () => ({
      sort: React.useSyncExternalStore(
        sortStore.subscribe,
        () => sortStore.value ?? actual.SIDEBAR_SORT_DEFAULT,
        () => sortStore.value ?? actual.SIDEBAR_SORT_DEFAULT,
      ),
      setSort: (next: typeof actual.SIDEBAR_SORT_DEFAULT) => sortStore.set(next),
    }),
  };
});

vi.mock("@/lib/transport/api", () => ({
  api: {
    patchAgentLabel: vi.fn().mockResolvedValue(undefined),
    listAgents: vi.fn(() =>
      Promise.resolve({
        agents: state.agents.filter((a) => a.status === "terminated"),
        next_cursor: null,
      }),
    ),
  },
}));

vi.mock("@/lib/plugins/plugin-stats", async () => {
  const actual = await vi.importActual<typeof PluginStatsModule>("@/lib/plugins/plugin-stats");
  return {
    ...actual,
    usePluginStatCards: (values: PluginStat[] | undefined) =>
      actual.buildPluginStatCards(state.pluginStatDeclarations, values),
  };
});

vi.mock("@/lib/state/use-user-settings", () => ({
  useUserSettings: () => ({
    settings: { "display.show_terminated": state.showTerminated, ...state.userSettings },
    setSetting: state.setSetting,
    isLoading: false,
  }),
}));

vi.mock("@/components/agents/agent-row", () => ({
  AgentRow: StubAgentRow,
}));

// meiva:note
// The sidebar only decides when the create form mounts; the form itself (the
// harness -> model -> effort table) is covered by spawn-form.test.tsx. The stub
// keeps aria-label="Spawn agent" so the sidebar tests stay about the sidebar.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
vi.mock("@/components/agents/spawn-form", () => ({
  SpawnForm: StubSpawnForm,
}));

vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: StubScrollArea,
}));

// meiva:note
// R4 layer 4: AgentSidebar mounts the desktop rail vs the mobile drawer by
// breakpoint (single source — useBreakpoint). Default to desktop; the
// mobile-drawer tests flip the mock. The block also carries the shared sort
// store: one value behind a subscription, not a per-hook useState, because the
// list and the settings popover read the same store in the app and a stub with
// two independent copies silently swallows the popover's writes.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
vi.mock("@/lib/layout/breakpoint", () => ({ useBreakpoint: () => bpMock() }));

vi.mock("@/components/ui/button", () => ({
  Button: StubButton,
}));

import { AgentSidebar } from "./index";
import { api } from "@/lib/transport/api";

afterEach(cleanup);
beforeEach(resetSidebarTestState);

describe("DesktopSidebar collapse/expand", () => {
  it("collapsed=true → the sidebar renders nothing at all", () => {
    state.sidebarCollapsed = true;
    state.pendingSpawnCount = 1;
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling" }),
      makeAgent({ agent_id: 2, status: "running" }),
    ];
    const { container } = wrap(<AgentSidebar {...handlers} />);
    expect(container.querySelector("aside")).toBeNull();
    expect(screen.queryByLabelText("Expand sidebar")).toBeNull();
    expect(screen.queryByText("#1")).toBeNull();
    expect(screen.queryByLabelText("Spawn agent")).toBeNull();
    expect(container.querySelectorAll(".animate-spin").length).toBe(0);
    expect(container.querySelectorAll(".animate-pulse").length).toBe(0);
  });

  it("collapsed=false → the collapse control sits on the sidebar's own edge", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Meiva")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Hide sidebar"));
    expect(state.setSidebarCollapsed).toHaveBeenCalledWith(true);
    expect(screen.queryByLabelText("Drag to resize width")).toBeNull();
  });

  it("leaves the vertical separator to the owning layout handle", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Meiva").closest("aside")?.className).not.toContain("border-r");
  });

  it("insets the expanded title divider", () => {
    wrap(<AgentSidebar {...handlers} />);
    const header = screen.getByText("Meiva").closest("header");
    for (const dividerClass of BAR_DIVIDER_CLASS.split(" ")) {
      expect(header?.className).toContain(dividerClass);
    }
    expect(header?.classList.contains("relative")).toBe(true);
    expect(header?.className).not.toContain("border-b");
  });

  it("expanded sidebar aside clips horizontal overflow (overflow-x-hidden backstop)", () => {
    wrap(<AgentSidebar {...handlers} />);
    const aside = screen.getByText("Meiva").closest("aside");
    expect(aside?.className).toContain("overflow-x-hidden");
  });

  it("expanded sidebar aside is a flex column (Task #1053 regression: R4-PR3 dropped display:flex, so the agent-list ScrollArea stopped bounding and the whole aside — header/bars/fixed footer — scrolled together)", () => {
    wrap(<AgentSidebar {...handlers} />);
    const aside = screen.getByText("Meiva").closest("aside");
    expect(aside?.className).toContain("flex");
    expect(aside?.className).toContain("flex-col");
  });

  it("header + opens the create form, and the form's Spawn button calls onSpawn", () => {
    wrap(<AgentSidebar {...handlers} />);
    // meiva:note
    // Two steps on purpose: the + only reveals the form (machine / model /
    // effort / harness), and the form's own Spawn button is what spawns.
    // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
    fireEvent.click(screen.getByTestId("sidebar-create"));
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(handlers.onSpawn).toHaveBeenCalled();
  });

  it("expanded desktop sidebar fills the resizable panel frame", () => {
    const { container } = wrap(<AgentSidebar {...handlers} />);
    const aside = container.querySelector<HTMLElement>("aside")!;
    expect(aside).toBeTruthy();
    expect(aside.className).toContain("w-full");
    expect(aside.style.width).toBe("");
  });
});

describe("MobileSidebar open/close", () => {
  beforeEach(() => {
    bpMock.mockReturnValue({ tier: "xs", isNarrow: true, isLarge: false });
  });

  it("mobileOpen=false → not rendered (null)", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByLabelText("Close sidebar")).toBeNull();
  });

  it("mobileOpen=true → renders overlay + close button", () => {
    state.mobileSidebarOpen = true;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByLabelText("Close sidebar")).toBeTruthy();
  });

  it("click close X → setMobileSidebarOpen(false)", () => {
    state.mobileSidebarOpen = true;
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Close sidebar"));
    expect(state.setMobileSidebarOpen).toHaveBeenCalledWith(false);
  });

  it("drawer header keeps create + display settings (the body is only a list)", () => {
    state.mobileSidebarOpen = true;
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("sidebar-settings")).toBeTruthy();
    fireEvent.click(screen.getByTestId("sidebar-create"));
    fireEvent.click(screen.getByLabelText("Spawn agent"));
    expect(handlers.onSpawn).toHaveBeenCalled();
  });
});

describe("SidebarBody empty / tree / spawning placeholder", () => {
  it("agents empty + no pending → empty state (no ul)", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByTestId("row-1")).toBeNull();
  });

  it("empty roster → the body says so and offers a full-width create button", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("No agents yet.")).toBeTruthy();
    // meiva:note
    // Nothing mounts before the click: the CTA is the only create control in
    // the body, and it opens the same form as the header's +.
    // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
    expect(screen.queryByLabelText("Spawn agent")).toBeNull();
    fireEvent.click(screen.getByTestId("sidebar-create-cta"));
    expect(screen.getByLabelText("Spawn agent")).toBeTruthy();
  });

  it("the list body carries no spawn bar and no display toolbar", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByLabelText("Spawn agent")).toBeNull();
    expect(screen.queryByText("Show terminated")).toBeNull();
    expect(screen.queryByText("Tree")).toBeNull();
  });

  it("agents non-empty → renders tree (one row per agent)", () => {
    state.agents = [
      makeAgent({ agent_id: 1, label: "first" }),
      makeAgent({ agent_id: 2, label: "second" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1")).toBeTruthy();
    expect(screen.getByTestId("row-2")).toBeTruthy();
  });

  it("tree mode + roster still loading with live rows present → loading state, no tree rows (no hierarchy flip)", () => {
    state.isLoading = true;
    state.agents = [
      makeAgent({ agent_id: 1, label: "first" }),
      makeAgent({ agent_id: 2, label: "second" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Loading agents…")).toBeTruthy();
    expect(screen.queryByTestId("row-1")).toBeNull();
    expect(screen.queryByTestId("row-2")).toBeNull();
  });

  it("flat mode shares the same coherent initial roster boundary", () => {
    state.isLoading = true;
    state.sidebarViewMode = "flat";
    state.agents = [
      makeAgent({ agent_id: 1, label: "first" }),
      makeAgent({ agent_id: 2, label: "second" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByTestId("row-1")).toBeNull();
    expect(screen.queryByTestId("row-2")).toBeNull();
  });

  it("pendingSpawnCount=2 → renders 2 SpawningRows, quiet by default (no motion)", () => {
    state.pendingSpawnCount = 2;
    const { container } = wrap(<AgentSidebar {...handlers} />);
    expect(screen.getAllByTestId("spawning-row").length).toBe(2);
    expect(container.querySelectorAll(".animate-spin").length).toBe(0);
    expect(container.querySelectorAll(".animate-pulse").length).toBe(0);
  });

  it("pendingSpawnCount + status colors enabled → SpawningRows animate", () => {
    state.userSettings = { "display.show_agent_status": true };
    state.pendingSpawnCount = 2;
    const { container } = wrap(<AgentSidebar {...handlers} />);
    expect(container.querySelectorAll(".animate-spin").length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThanOrEqual(2);
  });

  it("has pending → activeId masked to null, real rows lose active highlight", () => {
    state.agents = [makeAgent({ agent_id: 5 })];
    state.activeId = 5;
    state.pendingSpawnCount = 1;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-5").getAttribute("data-active")).toBe("0");
  });

  it("no pending + activeId=5 → row-5 active=1", () => {
    state.agents = [makeAgent({ agent_id: 5 })];
    state.activeId = 5;
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-5").getAttribute("data-active")).toBe("1");
  });

  it("agent in pendingActions → row receives pending prop", () => {
    state.agents = [makeAgent({ agent_id: 7 })];
    state.pendingActions = { 7: "terminating" };
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-7").getAttribute("data-pending")).toBe("terminating");
  });
});

describe("wide mode + fork propagation", () => {
  it("desktop rows render the full monitoring presentation", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1").getAttribute("data-wide")).toBe("1");
  });

  it("fork child nests under its source in the tree", () => {
    state.agents = [
      makeAgent({ agent_id: 1, spawner: "user" }),
      makeAgent({ agent_id: 2, spawner: "user", fork_source_agent_id: 1 }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("row-1").getAttribute("data-depth")).toBe("0");
    expect(screen.getByTestId("row-2").getAttribute("data-depth")).toBe("1");
  });

  it("mobile drawer renders wide regardless of sidebar width", () => {
    bpMock.mockReturnValue({ tier: "xs", isNarrow: true, isLarge: false });
    state.mobileSidebarOpen = true;
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    const row = screen.getByTestId("row-1");
    expect(row.getAttribute("data-wide")).toBe("1");
  });
});

describe("handleSelect and handleRename forwarding", () => {
  it("click row select → setActiveId(id) + setMobileSidebarOpen(false)", () => {
    state.agents = [makeAgent({ agent_id: 3 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("select-3"));
    expect(state.setActiveId).toHaveBeenCalledWith(3);
    expect(state.setMobileSidebarOpen).toHaveBeenCalledWith(false);
  });

  it("click row terminate → onTerminate prop called with agent id", () => {
    state.agents = [makeAgent({ agent_id: 4 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("terminate-4"));
    expect(handlers.onTerminate).toHaveBeenCalledWith(4);
  });

  it("click row restart/resurrect/fork → corresponding handler called with id", () => {
    state.agents = [makeAgent({ agent_id: 4 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("restart-4"));
    expect(handlers.onRestart).toHaveBeenCalledWith(4);
    fireEvent.click(screen.getByTestId("resurrect-4"));
    expect(handlers.onResurrect).toHaveBeenCalledWith(4, undefined);
    fireEvent.click(screen.getByTestId("fork-4"));
    expect(handlers.onFork).toHaveBeenCalledWith(4, undefined);
  });

  it("click row rename → api.patchAgentLabel(id, label) called", async () => {
    state.agents = [makeAgent({ agent_id: 9 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("rename-9"));
    await waitFor(() => expect(api.patchAgentLabel).toHaveBeenCalledWith(9, "new-label"));
  });
});
