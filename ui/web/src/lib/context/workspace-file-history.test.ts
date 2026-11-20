import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { fakeBrowser, pageWrapper, type FakeBrowser } from "@/lib/browser/testing";

import { PUSHED_ENTRY } from "./reference-url";
import { useWorkspaceFileHistory } from "./workspace-file-history";
import { useWorkspaceFileStore } from "./workspace-file-store";

// meiva:note
// The workspace file viewer moves through the page's history (user ruling 2026-11-20): every open is
// an entry, the browser's Back and Forward restore the file the address names, and a shared address
// opens the same file.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.md
const ORIGIN = "https://meiva.test";
const store = () => useWorkspaceFileStore.getState();

function mount(page: FakeBrowser, activeId: number | null = 7) {
  return renderHook(({ id }) => useWorkspaceFileHistory(id), { initialProps: { id: activeId }, wrapper: pageWrapper(page) });
}

afterEach(() => {
  useWorkspaceFileStore.setState({ activeFile: null, navigator: null, tabs: {}, recent: [], maximized: false });
});

describe("workspace file history", () => {
  it("pushes an entry per opened file, keeps the to-user chain, and replaces a new line range in place", () => {
    const page = fakeBrowser({ href: `${ORIGIN}/?agent_id=7&file=notes.md` });
    mount(page);
    act(() => store().openFile({ agentId: 7, path: "/w/src/main.py" }));
    expect(page.href()).toBe(`${ORIGIN}/?agent_id=7&file=notes.md&view=/w/src/main.py&view_agent=7`);
    expect(page.actions.pushed).toHaveLength(1);
    expect(Reflect.get(page.actions.pushed[0].state as object, PUSHED_ENTRY)).toBe(true);

    act(() => store().openFile({ agentId: 7, path: "/w/src/main.py", lineRange: "10-20" }));
    expect(page.actions.pushed).toHaveLength(1);
    expect(page.href()).toBe(`${ORIGIN}/?agent_id=7&file=notes.md&view=/w/src/main.py&view_agent=7&lines=10-20`);

    act(() => store().openFile({ agentId: 7, path: "/w/docs/readme.md" }));
    expect(page.actions.pushed).toHaveLength(2);
  });

  it("goes Back through the browser and restores the file the address names on Back and Forward", () => {
    const page = fakeBrowser({ href: `${ORIGIN}/?agent_id=7` });
    mount(page);
    act(() => store().openFile({ agentId: 7, path: "/w/a.py" }));
    const first = page.href();
    act(() => store().openFile({ agentId: 7, path: "/w/b.py" }));
    act(() => store().goBack());
    expect(page.actions.backs).toBe(1);
    act(() => page.traverse(first));
    expect(store().activeFile).toEqual({ agentId: 7, path: "/w/a.py" });
    act(() => page.traverse(`${ORIGIN}/?agent_id=7`));
    expect(store().activeFile).toBeNull();
    act(() => page.traverse(`${ORIGIN}/?agent_id=7&view=/w/b.py&view_agent=7&lines=3`));
    expect(store().activeFile).toEqual({ agentId: 7, path: "/w/b.py", lineRange: "3" });
  });

  it("opens the file a shared or reloaded address names, and keeps it through the first agent selection", () => {
    const page = fakeBrowser({ href: `${ORIGIN}/?agent_id=7&view=/w/report.md&view_agent=9` });
    const view = mount(page, null);
    expect(store().activeFile).toEqual({ agentId: 9, path: "/w/report.md" });
    view.rerender({ id: 7 });
    expect(store().activeFile).toEqual({ agentId: 9, path: "/w/report.md" });
    view.rerender({ id: 8 });
    expect(store().activeFile).toBeNull();
    act(() => store().openFile({ agentId: 8, path: "/w/c.md" }));
    view.rerender({ id: null });
    expect(store().activeFile?.path).toBe("/w/c.md");
    view.rerender({ id: 9 });
    expect(store().activeFile).toBeNull();
  });

  it("returns home as a new entry, and drops the file from the current entry when the panel closes", () => {
    const page = fakeBrowser({ href: `${ORIGIN}/?agent_id=7` });
    mount(page);
    act(() => store().openFile({ agentId: 7, path: "/w/a.py" }));
    act(() => store().home());
    expect(store().activeFile).toBeNull();
    expect(page.actions.pushed).toHaveLength(2);
    expect(page.href()).toBe(`${ORIGIN}/?agent_id=7`);

    act(() => store().openFile({ agentId: 7, path: "/w/a.py" }));
    act(() => store().closeFile());
    expect(store().activeFile).toBeNull();
    expect(page.actions.pushed).toHaveLength(3);
    expect(page.href()).toBe(`${ORIGIN}/?agent_id=7`);
  });

  it("closes the file in place when its entry was not one the panel pushed", () => {
    const page = fakeBrowser({ href: `${ORIGIN}/?agent_id=7&view=/w/a.py&view_agent=7` });
    mount(page);
    act(() => store().goBack());
    expect(page.actions.backs).toBe(0);
    expect(store().activeFile).toBeNull();
    expect(page.href()).toBe(`${ORIGIN}/?agent_id=7`);
  });

  // meiva:note
  // Every file opened in a conversation is a tab there; selecting one is an entry like opening it,
  // and closing the one on show moves to its neighbour in place.
  it("keeps opened files as tabs, selects them as entries and closes to the neighbour in place", () => {
    const page = fakeBrowser({ href: `${ORIGIN}/?agent_id=7` });
    mount(page);
    act(() => store().openFile({ agentId: 7, path: "/w/a.py" }));
    act(() => store().openFile({ agentId: 7, path: "/w/b.py" }));
    act(() => store().openFile({ agentId: 7, path: "/w/c.py" }));
    expect(store().tabs[7].map((tab) => tab.path)).toEqual(["/w/a.py", "/w/b.py", "/w/c.py"]);
    expect(store().recent.map((tab) => tab.path)).toEqual(["/w/c.py", "/w/b.py", "/w/a.py"]);

    act(() => store().selectTab(store().tabs[7][0]));
    expect(store().activeFile?.path).toBe("/w/a.py");
    expect(page.actions.pushed).toHaveLength(4);

    act(() => store().closeTab(store().tabs[7][0]));
    expect(store().activeFile?.path).toBe("/w/b.py");
    expect(page.actions.pushed).toHaveLength(4);
    expect(page.href()).toContain("view=/w/b.py");

    act(() => store().selectTab(null));
    expect(store().activeFile).toBeNull();
    expect(store().tabs[7]).toHaveLength(2);

    act(() => page.traverse(`${ORIGIN}/?agent_id=7&view=/w/d.py&view_agent=7`));
    expect(store().tabs[7].map((tab) => tab.path)).toEqual(["/w/b.py", "/w/c.py", "/w/d.py"]);
  });
});
