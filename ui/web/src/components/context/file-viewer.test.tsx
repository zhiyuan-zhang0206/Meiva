import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ReactNode } from "react";

import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { ApiError } from "@/lib/transport/api";
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
  const root = { kind: "workspace" as const, path: "/w", agent_id: 7 };
  const listing = (path: string, entries: ReturnType<typeof entry>[]) => ({ path, root, parent: null, entries, truncated: false });
  it("roots the path at the file's workspace and drops each folder down to open another file", async () => {
    transport.getWorkspaceFile.mockResolvedValue({ path: "/w/project/src/main.py", root, name: "main.py", extension: ".py", content: "x", size: 2048, binary: false, modified_at: "2026-11-20T12:00:00Z" });
    transport.listWorkspaceDir.mockImplementation((_agentId: number, path?: string) => Promise.resolve(
      path === "/w/project"
        ? listing("/w/project", [entry("src", "dir", "/w/project"), entry("README.md", "file", "/w/project")])
        : listing("/w/project/src", [entry("util.py", "file", "/w/project/src")]),
    ));
    store().openFile({ agentId: 7, path: "project/src/main.py" });
    renderWithBrowser(<PathBar agentId={7} documentModifiedAt={null} />, { wrapper: Wrapper });
    await waitFor(() => expect(screen.getByTestId("path-bar").textContent).toContain("2.0 KB"));
    expect(screen.getByRole("button", { name: "#7" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "project" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "src" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "util.py" }));
    expect(store().activeFile).toEqual({ agentId: 7, path: "/w/project/src/util.py" });
    expect(screen.queryByTestId("path-menu")).toBeNull();
  });

  it("names a file under the home directory from ~ and one elsewhere from /", async () => {
    transport.getWorkspaceFile.mockResolvedValueOnce({ path: "/home/u/notes/a.md", root: { kind: "home", path: "/home/u", agent_id: null }, name: "a.md", extension: ".md", content: "x", size: 1, binary: false, modified_at: "2026-11-20T12:00:00Z" });
    store().openFile({ agentId: 7, path: "~/notes/a.md" });
    const view = renderWithBrowser(<PathBar agentId={7} documentModifiedAt={null} />, { wrapper: Wrapper });
    expect(await screen.findByRole("button", { name: "~" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "notes" })).toBeTruthy();
    view.unmount();
    transport.getWorkspaceFile.mockResolvedValueOnce({ path: "/etc/hosts", root: { kind: "machine", path: "/", agent_id: null }, name: "hosts", extension: "", content: "x", size: 1, binary: false, modified_at: "2026-11-20T12:00:00Z" });
    store().openFile({ agentId: 7, path: "/etc/hosts" });
    renderWithBrowser(<PathBar agentId={7} documentModifiedAt={null} />, { wrapper: Wrapper });
    expect(await screen.findByRole("button", { name: "/" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "etc" })).toBeTruthy();
  });

  it("roots a path that does not resolve as it was written", async () => {
    transport.getWorkspaceFile.mockRejectedValue(new Error("not found"));
    store().openFile({ agentId: 7, path: "~/missing/a.md" });
    renderWithBrowser(<PathBar agentId={7} documentModifiedAt={null} />, { wrapper: Wrapper });
    await waitFor(() => expect(transport.getWorkspaceFile).toHaveBeenCalled());
    const bar = screen.getByTestId("path-bar");
    expect([...bar.querySelectorAll("button[aria-haspopup]")].map((b) => b.textContent)).toEqual(["~", "missing"]);
  });

  // meiva:note
  // A normal address bar: clicking its empty end edits the path; Enter opens a file as a tab, or a
  // folder as its drop-down; Escape gives up.
  it("edits the path in place and opens what Enter names", async () => {
    transport.getWorkspaceFile.mockResolvedValue({ path: "/home/u/a.md", root: { kind: "home", path: "/home/u", agent_id: null }, name: "a.md", extension: ".md", content: "x", size: 1, binary: false, modified_at: "2026-11-20T12:00:00Z" });
    transport.listWorkspaceDir.mockImplementation((_agentId: number, path?: string) => (path === "~/docs" || path === "/home/u/docs"
      ? Promise.resolve({ path: "/home/u/docs", root: { kind: "home" as const, path: "/home/u", agent_id: null }, parent: "/home/u", entries: [entry("b.md", "file", "/home/u/docs")], truncated: false })
      : Promise.reject(new ApiError(404, "not a directory", { code: "http_404", retryable: false }))));
    store().openFile({ agentId: 7, path: "~/a.md" });
    renderWithBrowser(<PathBar agentId={7} documentModifiedAt={null} />, { wrapper: Wrapper });
    await screen.findByRole("button", { name: "~" });
    fireEvent.click(screen.getByRole("button", { name: "Edit the path" }));
    const input = screen.getByRole<HTMLInputElement>("textbox", { name: "Path" });
    expect(input.value).toBe("~/a.md");
    fireEvent.change(input, { target: { value: "~/docs" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "b.md" }));
    expect(store().activeFile).toEqual({ agentId: 7, path: "/home/u/docs/b.md" });

    fireEvent.click(screen.getByRole("button", { name: "Edit the path" }));
    const again = screen.getByRole<HTMLInputElement>("textbox", { name: "Path" });
    fireEvent.change(again, { target: { value: "/etc/hosts" } });
    fireEvent.keyDown(again, { key: "Enter" });
    await waitFor(() => expect(store().activeFile).toEqual({ agentId: 7, path: "/etc/hosts" }));
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
        ? { path: "/w/src", root: { kind: "workspace" as const, path: "/w", agent_id: 7 }, parent: "/w", entries: [entry("main.py", "file", "/w/src")], truncated: false }
        : { path: agentId === 7 ? "/w" : "/v", root: { kind: "workspace" as const, path: agentId === 7 ? "/w" : "/v", agent_id: agentId }, parent: null, entries: agentId === 7 ? [entry("src", "dir"), entry("a.md", "file")] : [entry("b.md", "file", "/v")], truncated: false },
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
    expect(store().activeFile).toEqual({ agentId: 7, machineAgentId: 405, path: "/v/b.md" });
    expect(transport.listWorkspaceDir).toHaveBeenCalledWith(405, undefined, expect.anything());
  });
});
