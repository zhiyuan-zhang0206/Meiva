import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { BAR_DIVIDER_CLASS } from "@/lib/layout/layout";
import { useStore } from "@/lib/state/store";
import { transport } from "@/test-support/fake-api";

import {
  handlers,
  installMenuPolyfills,
  makeAgent,
  NARROW_MEDIA,
  openRowMenu,
  queryRow,
  resetSidebarTestState,
  rowButton,
  rowItem,
  state,
  treeGuideDepths,
  wrap,
} from "./sidebar-test-mock";

// meiva:note
// Every input arrives as a value and no module is replaced: the roster through props, preferences
// through the settings cache, the drawer/search selection through the store, the viewport through
// the fake browser, and the route change through the navigation value the harness hands in.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { AgentSidebar } from "./index";

beforeAll(installMenuPolyfills);
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
    expect(screen.queryByRole("button", { name: "Spawn" })).toBeNull();
    expect(container.querySelectorAll(".animate-spin").length).toBe(0);
    expect(container.querySelectorAll(".animate-pulse").length).toBe(0);
  });

  it("collapsed=false → the collapse control sits on the sidebar's own edge", async () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Meiva")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Hide sidebar"));
    await waitFor(() =>
      expect(transport.putSetting).toHaveBeenCalledWith("display.sidebar_collapsed", true),
    );
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

  it("header + opens the real create form, and its Spawn button calls onSpawn", async () => {
    wrap(<AgentSidebar {...handlers} />);
    // meiva:note
    // Two steps on purpose: the + only reveals the form (machine / model /
    // effort / harness), and the form's own Spawn button is what spawns.
    // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
    fireEvent.click(screen.getByTestId("sidebar-create"));
    fireEvent.click(await screen.findByRole("button", { name: "Spawn" }));
    await waitFor(() => expect(handlers.onSpawn).toHaveBeenCalled());
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
    state.media = NARROW_MEDIA;
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
    expect(useStore.getState().mobileSidebarOpen).toBe(false);
  });

  it("drawer body renders the roster as real rows", () => {
    state.mobileSidebarOpen = true;
    state.agents = [makeAgent({ agent_id: 11 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(queryRow(11)).toBeTruthy();
    expect(rowItem(11).querySelector("button")).toBe(rowButton(11));
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("drawer header keeps create + display settings (the body is only a list)", async () => {
    state.mobileSidebarOpen = true;
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByTestId("sidebar-settings")).toBeTruthy();
    fireEvent.click(screen.getByTestId("sidebar-create"));
    fireEvent.click(await screen.findByRole("button", { name: "Spawn" }));
    await waitFor(() => expect(handlers.onSpawn).toHaveBeenCalled());
  });
});

describe("SidebarBody empty / tree / spawning placeholder", () => {
  it("agents empty + no pending → empty state (no list items)", () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("empty roster → the body says so and offers a full-width create button", async () => {
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("No agents yet.")).toBeTruthy();
    // meiva:note
    // Nothing mounts before the click: the CTA is the only create control in
    // the body, and it opens the same form as the header's +.
    // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
    expect(screen.queryByRole("button", { name: "Spawn" })).toBeNull();
    fireEvent.click(screen.getByTestId("sidebar-create-cta"));
    expect(await screen.findByRole("button", { name: "Spawn" })).toBeTruthy();
  });

  it("the list body carries no spawn bar and no display toolbar", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByRole("button", { name: "Spawn" })).toBeNull();
    expect(screen.queryByText("Show terminated")).toBeNull();
    expect(screen.queryByText("Tree")).toBeNull();
  });

  it("agents non-empty → renders tree (one row per agent)", () => {
    state.agents = [
      makeAgent({ agent_id: 1, label: "first" }),
      makeAgent({ agent_id: 2, label: "second" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(rowButton(1).textContent).toContain("#1 · first");
    expect(rowButton(2).textContent).toContain("#2 · second");
  });

  it("tree mode + roster still loading with live rows present → loading state, no tree rows (no hierarchy flip)", () => {
    state.isLoading = true;
    state.agents = [
      makeAgent({ agent_id: 1, label: "first" }),
      makeAgent({ agent_id: 2, label: "second" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.getByText("Loading agents…")).toBeTruthy();
    expect(screen.queryByText(/#1 · first/)).toBeNull();
    expect(screen.queryByText(/#2 · second/)).toBeNull();
  });

  it("flat mode shares the same coherent initial roster boundary", () => {
    state.isLoading = true;
    state.sidebarViewMode = "flat";
    state.agents = [
      makeAgent({ agent_id: 1, label: "first" }),
      makeAgent({ agent_id: 2, label: "second" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByText(/#1 · first/)).toBeNull();
    expect(screen.queryByText(/#2 · second/)).toBeNull();
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
    expect(rowButton(5).classList.contains("bg-sidebar-accent")).toBe(false);
  });

  it("no pending + activeId=5 → the row carries the active highlight", () => {
    state.agents = [makeAgent({ agent_id: 5 })];
    state.activeId = 5;
    wrap(<AgentSidebar {...handlers} />);
    expect(rowButton(5).classList.contains("bg-sidebar-accent")).toBe(true);
  });

  it("agent in pendingActions → the row shows its terminating spinner", () => {
    state.agents = [makeAgent({ agent_id: 7 })];
    state.pendingActions = { 7: "terminating" };
    wrap(<AgentSidebar {...handlers} />);
    const spinner = rowItem(7).querySelector(".animate-spin");
    expect(spinner).not.toBeNull();
    expect(spinner?.getAttribute("class")).toContain("text-destructive");
  });
});

describe("tree nesting", () => {
  // meiva:note
  // `wide` used to be asserted through the stub row's data-wide. The real row declares the prop
  // and never reads it (`agent-row.tsx`), so there is no rendered signal to assert and the
  // attribute was the stub's own invention; the dead prop is reported for removal separately.
  it("fork child nests under its source in the tree", () => {
    state.agents = [
      makeAgent({ agent_id: 1, spawner: "user" }),
      makeAgent({ agent_id: 2, spawner: "user", fork_source_agent_id: 1 }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(treeGuideDepths(1)).toEqual([]);
    expect(treeGuideDepths(2)).toEqual([0]);
  });
});

describe("handleSelect and lifecycle forwarding", () => {
  it("click row → activeId lands in the store and the drawer closes", () => {
    state.agents = [makeAgent({ agent_id: 3 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(rowButton(3));
    expect(useStore.getState().activeId).toBe(3);
    expect(useStore.getState().mobileSidebarOpen).toBe(false);
  });

  it("row menu Terminate → onTerminate prop called with the agent id", async () => {
    state.agents = [makeAgent({ agent_id: 4 })];
    wrap(<AgentSidebar {...handlers} />);
    openRowMenu(4);
    fireEvent.click(await screen.findByText("Terminate"));
    expect(handlers.onTerminate).toHaveBeenCalledWith(4);
  });

  it("row menu Restart → onRestart, and Fork → onFork with no prompt", async () => {
    state.agents = [makeAgent({ agent_id: 4 })];
    wrap(<AgentSidebar {...handlers} />);
    openRowMenu(4);
    fireEvent.click(await screen.findByText("Restart"));
    expect(handlers.onRestart).toHaveBeenCalledWith(4);
    openRowMenu(4);
    fireEvent.click(await screen.findByText("Fork"));
    expect(handlers.onFork).toHaveBeenCalledWith(4, undefined);
  });

  it("row menu Resurrect on a terminated row → onResurrect with no prompt", async () => {
    // meiva:note
    // A terminated agent is the archive's row, not a tree node: the toggle opens the list
    // that reads it, and its menu is the only place Resurrect lives.
    state.showTerminated = true;
    state.agents = [makeAgent({ agent_id: 4, status: "terminated" })];
    wrap(<AgentSidebar {...handlers} />);
    await screen.findByText("#4");
    openRowMenu(4);
    fireEvent.click(await screen.findByText("Resurrect"));
    expect(handlers.onResurrect).toHaveBeenCalledWith(4, undefined);
  });

  it("rename in place → transport.patchAgentLabel(id, label) called", async () => {
    state.agents = [makeAgent({ agent_id: 9 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.doubleClick(screen.getByText("#9"));
    const field = screen.getByLabelText("Rename agent 9");
    fireEvent.change(field, { target: { value: "new-label" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(transport.patchAgentLabel).toHaveBeenCalledWith(9, "new-label"));
  });
});
