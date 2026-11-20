import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ReactNode } from "react";

import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { resetTransport, transport } from "@/test-support/fake-api";
import { TestStreamProvider } from "@/test-support/fake-stream";
import { renderWithBrowser } from "@/test-support/render";

import { FileTree } from "./file-tree";
import { PanelBar } from "./panel-bar";
import { PathBar } from "./path-bar";

// meiva:note
// The panel as a file viewer (user rulings 2026-11-20 / 2026-11-21): one bar of tabs and controls,
// a path bar whose folders drop down like a file manager's, and a tree for the full-page viewer that
// can browse another agent's workspace.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
let qc: QueryClient;
function Wrapper({ children }: { children: ReactNode }) {
  return (
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>
        <TestStreamProvider>{children}</TestStreamProvider>
      </QueryClientProvider>
    </WallClockProvider>
  );
}
const store = () => useWorkspaceFileStore.getState();
const entry = (name: string, kind: "dir" | "file", dir = "/w") => ({ name, path: `${dir}/${name}`, kind, size: kind === "file" ? 10 : null, modified_at: "2026-11-20T12:00:00Z" });

beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  resetTransport();
  useWorkspaceFileStore.setState({ activeFile: null, tabs: {}, recent: [], maximized: false, navigator: null });
});
afterEach(cleanup);

describe("panel bar", () => {
  it("shows the document first, then each opened file without a type icon, and holds the panel's controls", () => {
    store().openFile({ agentId: 7, path: "/w/src/main.py" });
    store().openFile({ agentId: 7, path: "/w/notes.md" });
    const inspected: string[] = [];
    renderWithBrowser(<PanelBar agentId={7} mobile={false} inspecting={false} onInspect={() => inspected.push("inspect")} onCollapse={() => inspected.push("collapse")} />, { wrapper: Wrapper });
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Shared context", "main.py", "notes.md"]);
    expect(tabs[2].getAttribute("aria-selected")).toBe("true");
    expect(tabs[2].querySelector("svg")).toBeNull();
    expect(screen.queryByRole("button", { name: "Home" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Close notes.md" }));
    expect(store().activeFile?.path).toBe("/w/src/main.py");
    fireEvent.click(screen.getByRole("tab", { name: "Shared context" }));
    expect(store().activeFile).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Fill the page" }));
    expect(store().maximized).toBe(true);
    fireEvent.click(screen.getByTestId("open-inspector"));
    fireEvent.click(screen.getByTestId("context-panel-collapse"));
    expect(inspected).toEqual(["inspect", "collapse"]);
  });
});

describe("path bar", () => {
  it("names the file's folders and drops each one down to open another file", async () => {
    transport.getWorkspaceFile.mockResolvedValue({ path: "project/src/main.py", name: "main.py", extension: ".py", content: "x", size: 2048, binary: false, modified_at: "2026-11-20T12:00:00Z" });
    transport.listWorkspaceDir.mockImplementation((agentId: number, path?: string) => Promise.resolve(
      path === "project"
        ? { path: "/w/project", parent: "/w", entries: [entry("src", "dir", "/w/project"), entry("README.md", "file", "/w/project")], truncated: false }
        : path === "/w/project/src"
          ? { path: "/w/project/src", parent: "/w/project", entries: [entry("util.py", "file", "/w/project/src")], truncated: false }
          : { path: agentId === 7 ? "/w" : "/v", parent: null, entries: agentId === 7 ? [entry("project", "dir")] : [entry("b.md", "file", "/v")], truncated: false },
    ));
    store().openFile({ agentId: 7, path: "project/src/main.py" });
    renderWithBrowser(<PathBar agentId={7} documentModifiedAt={null} />, { wrapper: Wrapper });
    const bar = screen.getByTestId("path-bar");
    expect(bar.textContent).toContain("#7");
    expect(bar.textContent).toContain("project");
    expect(bar.textContent).toContain("main.py");
    await waitFor(() => expect(bar.textContent).toContain("2.0 KB"));

    fireEvent.click(screen.getByRole("button", { name: "project" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "src" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "util.py" }));
    expect(store().activeFile).toEqual({ agentId: 7, path: "/w/project/src/util.py" });
    expect(screen.queryByTestId("path-menu")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "#7" }));
    const number = await screen.findByRole("textbox", { name: "Agent number" });
    fireEvent.change(number, { target: { value: "405" } });
    fireEvent.submit(number.closest("form")!);
    fireEvent.click(await screen.findByRole("menuitem", { name: "b.md" }));
    expect(store().activeFile).toEqual({ agentId: 7, path: "/v/b.md" });
  });

  it("shows when the to-user document last changed on the document tab", () => {
    renderWithBrowser(<PathBar agentId={7} documentModifiedAt={new Date(Date.now() - 5 * 60_000).toISOString()} />, { wrapper: Wrapper });
    const bar = screen.getByTestId("path-bar");
    expect(bar.textContent).toContain("to_user.md");
    expect(bar.textContent).toMatch(/Updated/);
  });
});

describe("file tree", () => {
  it("opens folders on demand, opens files in this conversation, and switches to another agent's workspace", async () => {
    transport.listWorkspaceDir.mockImplementation((agentId: number, path?: string) => Promise.resolve(
      path === "/w/src"
        ? { path: "/w/src", parent: "/w", entries: [entry("main.py", "file", "/w/src")], truncated: false }
        : { path: agentId === 7 ? "/w" : "/v", parent: null, entries: agentId === 7 ? [entry("src", "dir"), entry("a.md", "file")] : [entry("b.md", "file", "/v")], truncated: false },
    ));
    const switched: number[] = [];
    const view = renderWithBrowser(<FileTree agentId={7} workspaceAgentId={7} onWorkspaceChange={(id) => switched.push(id)} />, { wrapper: Wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "src" }));
    fireEvent.click(await screen.findByRole("button", { name: "main.py" }));
    expect(store().activeFile).toEqual({ agentId: 7, path: "/w/src/main.py" });

    fireEvent.change(screen.getByRole("textbox", { name: "Agent number" }), { target: { value: "405" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "Agent number" }).closest("form")!);
    expect(switched).toEqual([405]);
    view.rerender(<FileTree agentId={7} workspaceAgentId={405} onWorkspaceChange={() => undefined} />);
    fireEvent.click(await screen.findByRole("button", { name: "b.md" }));
    expect(store().activeFile).toEqual({ agentId: 7, path: "/v/b.md" });
    expect(transport.listWorkspaceDir).toHaveBeenCalledWith(405, undefined, expect.anything());
  });
});
