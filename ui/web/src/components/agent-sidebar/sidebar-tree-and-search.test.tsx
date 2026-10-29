import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { SIDEBAR_SORT_DEFAULT } from "@/lib/agents/sidebar";
import type { OpenNotice } from "@/lib/contracts/types";
import { useStore } from "@/lib/state/store";
import { transport } from "@/test-support/fake-api";

import {
  handlers,
  installMenuPolyfills,
  makeAgent,
  pushSpy,
  resetSidebarTestState,
  queryRow,
  rowOrder,
  state,
  treeGuideDepths,
  wrap,
} from "./sidebar-test-mock";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushSpy }),
}));

import { AgentSidebar } from "./index";

beforeAll(installMenuPolyfills);
afterEach(cleanup);
beforeEach(resetSidebarTestState);

describe("Tree render depth (sub-agent indent)", () => {
  it("forked agent → renders indented under its spawner (depth=1)", () => {
    state.agents = [
      makeAgent({ agent_id: 1, spawner: "user" }),
      makeAgent({ agent_id: 2, spawner: "agent:1", fork_source_agent_id: 1 }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(treeGuideDepths(1)).toEqual([]);
    expect(treeGuideDepths(2)).toEqual([0]);
  });
});

describe("terminated toggle (behind the sidebar settings popover)", () => {
  // meiva:note
  // The display toolbar that sat under the tree is gone: the archive toggle now
  // lives in the settings popover, so every case opens it first.
  // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
  const openSettings = () => fireEvent.click(screen.getByTestId("sidebar-settings"));
  const toggle = (name: "Show terminated agents" | "Hide terminated agents") =>
    screen.getByRole("button", { name });
  it("archive is reachable without preloading terminated agents", () => {
    state.agents = [makeAgent({ agent_id: 1, status: "idling" })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByRole("button", { name: "Show terminated agents" })).toBeNull();
    openSettings();
    expect(toggle("Show terminated agents")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Hide terminated agents" })).toBeNull();
  });
  it("showTerminated=false → toggle reads 'Show terminated agents', terminated rows hidden, live row stays", () => {
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling" }),
      makeAgent({ agent_id: 2, status: "terminated" }),
      makeAgent({ agent_id: 3, status: "terminated" }),
    ];
    state.showTerminated = false;
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    expect(toggle("Show terminated agents").textContent).not.toMatch(/\d/);
    expect(queryRow(1)).not.toBeNull();
    expect(queryRow(2)).toBeNull();
    expect(queryRow(3)).toBeNull();
  });
  it("opening history fetches bounded archived rows separately", async () => {
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling" }),
      makeAgent({ agent_id: 2, status: "terminated" }),
    ];
    state.showTerminated = true;
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    expect(toggle("Hide terminated agents")).toBeTruthy();
    await waitFor(() => expect(queryRow(2)).not.toBeNull());
  });
  it("malformed persisted setting stays opt-out", () => {
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling" }),
      makeAgent({ agent_id: 2, status: "terminated" }),
    ];
    state.userSettings["display.show_terminated"] = "true";
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    expect(toggle("Show terminated agents")).toBeTruthy();
    expect(queryRow(2)).toBeNull();
  });
  it("all agents terminated + hidden → toggle still shows so they can be revealed", () => {
    state.agents = [makeAgent({ agent_id: 2, status: "terminated" })];
    state.showTerminated = false;
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    expect(toggle("Show terminated agents")).toBeTruthy();
    expect(queryRow(2)).toBeNull();
  });
  it("click toggle → setSetting(display.show_terminated, !showTerminated)", async () => {
    state.agents = [makeAgent({ agent_id: 2, status: "terminated" })];
    state.showTerminated = false;
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    fireEvent.click(toggle("Show terminated agents"));
    await waitFor(() =>
      expect(transport.putSetting).toHaveBeenCalledWith("display.show_terminated", true),
    );
  });
  it("toggle button carries no title tooltip (the visible label covers a11y)", () => {
    state.agents = [makeAgent({ agent_id: 2, status: "terminated" })];
    state.showTerminated = false;
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    expect(toggle("Show terminated agents").getAttribute("title")).toBeNull();
  });
});

describe("agent tree: live child of a terminated parent mounts on the nearest visible ancestor (#312 regression)", () => {
  const lineageRoster = () => [
    makeAgent({ agent_id: 228, spawner: "user", status: "idling" }),
    makeAgent({ agent_id: 240, spawner: "agent:228", status: "terminated" }),
    makeAgent({ agent_id: 312, spawner: "agent:240", status: "idling" }),
  ];
  it("terminated hidden (default): 312 nests under 228, no orphan root, 240 row hidden", () => {
    state.showTerminated = false;
    state.agents = lineageRoster();
    wrap(<AgentSidebar {...handlers} />);
    expect(treeGuideDepths(228)).toEqual([]);
    expect(treeGuideDepths(312)).toEqual([0]);
    expect(queryRow(240)).toBeNull();
  });
  it("opening archive preserves the live tree and exposes the historical row separately", async () => {
    state.showTerminated = true;
    state.agents = lineageRoster();
    wrap(<AgentSidebar {...handlers} />);
    expect(treeGuideDepths(228)).toEqual([]);
    await waitFor(() => expect(queryRow(240)).not.toBeNull());
    expect(treeGuideDepths(240)).toEqual([]);
    expect(treeGuideDepths(312)).toEqual([0]);
  });
});

describe("Flat view mode", () => {
  it("sort lives behind the sidebar settings button, not in a toolbar", () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 1 }), makeAgent({ agent_id: 2 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByLabelText("Sort by id descending")).toBeNull();
    fireEvent.click(screen.getByTestId("sidebar-settings"));
    expect(screen.getByText("ID")).toBeTruthy();
  });
  it("sort popover: opening shows the three keys; clicking the active key flips direction", async () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 2 }), makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByTestId("sidebar-settings"));
    expect(screen.getByText("Sort by")).toBeTruthy();
    expect(screen.getByText("ID")).toBeTruthy();
    expect(screen.getByText("Activity")).toBeTruthy();
    expect(screen.getByText("Status")).toBeTruthy();
    fireEvent.click(screen.getByText("ID"));
    // meiva:note
    // Clicking the active key again flips the direction; the row shows the arrow.
    // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
    await waitFor(() =>
      expect(screen.getByText("ID").closest("button")?.textContent).toContain("↑"),
    );
  });
  it("renders agents as flat list when viewMode=flat", () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 1 }), makeAgent({ agent_id: 2 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(queryRow(1)).not.toBeNull();
    expect(queryRow(2)).not.toBeNull();
  });
  it("defaults to id descending; clicking the active key reverses direction", async () => {
    state.sidebarViewMode = "flat";
    state.agents = [makeAgent({ agent_id: 2 }), makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(rowOrder()).toEqual(["2", "1"]);
    fireEvent.click(screen.getByTestId("sidebar-settings"));
    fireEvent.click(screen.getByText("ID"));
    await waitFor(() => expect(rowOrder()).toEqual(["1", "2"]));
  });
});

describe("Header layout + search overlay (task #723)", () => {
  it("expanded header: search, settings, create, collapse - one row, four icons", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    const header = screen.getByText("Meiva").closest("header")!;
    const labels = Array.from(header.querySelectorAll("button")).map((b) =>
      b.getAttribute("aria-label"),
    );
    expect(labels.filter(Boolean)).toEqual([
      "Search agents",
      "Sidebar settings",
      "Create agent",
      "Hide sidebar",
    ]);
  });
  it("clicking the search button opens the floating overlay", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByLabelText("Search agent")).toBeNull();
    fireEvent.click(screen.getByLabelText("Search agents"));
    expect(screen.getByLabelText("Search agent")).toBeTruthy();
  });
  it("typing pushes the query to the store; picking a result selects + closes", () => {
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    expect(screen.getByTestId("overlay-result-1")).toBeTruthy();
    expect(screen.getByTestId("overlay-result-2")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search agent"), { target: { value: "beta" } });
    expect(useStore.getState().searchQuery).toBe("beta");
    fireEvent.click(screen.getByTestId("overlay-result-2"));
    expect(useStore.getState().activeId).toBe(2);
    expect(screen.queryByLabelText("Search agent")).toBeNull();
  });
  it("Esc closes the overlay", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByLabelText("Search agent")).toBeNull();
  });
  it("clicking the dimmed backdrop closes the overlay", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    fireEvent.click(screen.getByTestId("search-overlay-backdrop"));
    expect(screen.queryByLabelText("Search agent")).toBeNull();
  });
  it("mount with sidebarCollapsed=true preserves the active session choice", () => {
    state.sidebarCollapsed = true;
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(transport.putSetting).not.toHaveBeenCalledWith(
      "display.sidebar_collapsed",
      expect.anything(),
    );
  });
});

describe("Search is overlay-only (task #750: sidebar list is NOT filtered)", () => {
  it("searchQuery empty → sidebar shows all agents", () => {
    state.searchQuery = "";
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(queryRow(1)).not.toBeNull();
    expect(queryRow(2)).not.toBeNull();
  });
  it("searchQuery matches a label → sidebar still shows every agent", () => {
    state.searchQuery = "alpha";
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(queryRow(1)).not.toBeNull();
    expect(queryRow(2)).not.toBeNull();
  });
  it("searchQuery matches an id → sidebar still shows every agent", () => {
    state.searchQuery = "2";
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(queryRow(1)).not.toBeNull();
    expect(queryRow(2)).not.toBeNull();
  });
  it("searchQuery matches nothing → sidebar still shows every agent", () => {
    state.searchQuery = "nonexistent";
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(queryRow(1)).not.toBeNull();
  });
  it("overlay results are agent-id DESCENDING (task #750)", () => {
    state.agents = [
      makeAgent({ agent_id: 1, label: "alpha" }),
      makeAgent({ agent_id: 3, label: "gamma" }),
      makeAgent({ agent_id: 2, label: "beta" }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    const results = screen
      .getAllByTestId(/^overlay-result-/)
      .map((el) => Number(el.getAttribute("data-testid")!.replace("overlay-result-", "")));
    expect(results).toEqual([3, 2, 1]);
  });
  it("overlay row shows `#id · label`, matching sidebar rows (task #750)", () => {
    state.agents = [makeAgent({ agent_id: 7, label: "worker" })];
    wrap(<AgentSidebar {...handlers} />);
    fireEvent.click(screen.getByLabelText("Search agents"));
    expect(screen.getByTestId("overlay-result-7").textContent).toContain("#7 · worker");
  });
});

describe("agent status quick toggle (display.show_agent_status)", () => {
  const openSettings = () => fireEvent.click(screen.getByTestId("sidebar-settings"));
  it("defaults off → the settings popover offers the 'Show agent status' toggle", () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    const btn = screen.getByRole("button", { name: "Show agent status" });
    expect(btn.getAttribute("aria-pressed")).toBe("false");
  });
  it("click toggle (off) → setSetting('display.show_agent_status', true)", async () => {
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Show agent status" }));
    await waitFor(() =>
      expect(transport.putSetting).toHaveBeenCalledWith("display.show_agent_status", true),
    );
  });
  it("enabled → toggle reads 'Hide agent status'; click → setSetting(false)", async () => {
    state.userSettings = { "display.show_agent_status": true };
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    openSettings();
    const btn = screen.getByRole("button", { name: "Hide agent status" });
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(btn);
    await waitFor(() =>
      expect(transport.putSetting).toHaveBeenCalledWith("display.show_agent_status", false),
    );
  });
});

describe("awaiting-reply indicator (notification.awaiting_reply)", () => {
  const notice: OpenNotice = {
    id: 1,
    title: "Q",
    content: null,
    priority: "P2",
    require_response: true,
    blocking: false,
    created_at: "2026-06-06T00:00:00Z",
  };
  it("default off → no waiting indicator even when agents have open notices", () => {
    state.agents = [
      makeAgent({
        agent_id: 1,
        awaiting_response_count: [notice].length,
        highest_notice_priority: [notice][0]?.priority ?? null,
      }),
    ];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByTitle(/waiting on you/)).toBeNull();
  });
  it("opted in → red-dot indicator with the cross-agent count", () => {
    state.userSettings = { "notification.awaiting_reply": true };
    state.agents = [
      makeAgent({
        agent_id: 1,
        awaiting_response_count: [notice].length,
        highest_notice_priority: [notice][0]?.priority ?? null,
      }),
      makeAgent({
        agent_id: 2,
        awaiting_response_count: [{ ...notice, id: 2 }].length,
        highest_notice_priority: [{ ...notice, id: 2 }][0]?.priority ?? null,
      }),
    ];
    state.activeId = 2;
    wrap(<AgentSidebar {...handlers} />);
    const link = screen.getByRole("link", { name: /2/ });
    expect(link.getAttribute("href")).toBe("/fleet?agent_id=2");
  });
  it("opted in but nothing waiting → no indicator", () => {
    state.userSettings = { "notification.awaiting_reply": true };
    state.agents = [makeAgent({ agent_id: 1 })];
    wrap(<AgentSidebar {...handlers} />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("stable ID ordering (RCS: no resort on status / activity change)", () => {
  it("default sort is pinned to id descending", () => {
    expect(SIDEBAR_SORT_DEFAULT).toEqual({ key: "id", dir: "desc" });
  });
  it("status + last_active_at changes never reorder rows under the default sort", () => {
    state.sidebarViewMode = "flat";
    state.agents = [
      makeAgent({ agent_id: 1, status: "idling", last_active_at: "2026-05-15T03:00:00Z" }),
      makeAgent({ agent_id: 2, status: "idling", last_active_at: "2026-05-15T02:00:00Z" }),
      makeAgent({ agent_id: 3, status: "idling", last_active_at: "2026-05-15T01:00:00Z" }),
    ];
    const view = wrap(<AgentSidebar {...handlers} />);
    expect(rowOrder()).toEqual(["3", "2", "1"]);
    state.agents = [
      makeAgent({ agent_id: 1, status: "running", last_active_at: "2026-05-16T00:00:00Z" }),
      makeAgent({ agent_id: 2, status: "idling", last_active_at: "2026-05-15T02:00:00Z" }),
      makeAgent({ agent_id: 3, status: "idling", last_active_at: "2026-05-15T01:00:00Z" }),
    ];
    view.rerender(<AgentSidebar {...handlers} />);
    expect(rowOrder()).toEqual(["3", "2", "1"]);
  });
});
