import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ReactNode } from "react";

import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { resetTransport, transport } from "@/test-support/fake-api";
import { TestStreamProvider } from "@/test-support/fake-stream";
import { renderWithBrowser } from "@/test-support/render";

import { FileTabs } from "./file-tabs";
import { FileTree } from "./file-tree";
import { QuickOpen } from "./quick-open";

// meiva:note
// The panel as a file viewer (user ruling 2026-11-20): tabs per conversation, quick open over the
// workspace, and a tree that can browse another agent's workspace.
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

describe("file tabs", () => {
  it("shows the document first, then each opened file without a type icon, and closes a tab", () => {
    store().openFile({ agentId: 7, path: "/w/src/main.py" });
    store().openFile({ agentId: 7, path: "/w/notes.md" });
    renderWithBrowser(<FileTabs agentId={7} mobile={false} treeOpen={false} onToggleTree={() => undefined} onQuickOpen={() => undefined} />, { wrapper: Wrapper });
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Shared context", "main.py", "notes.md"]);
    expect(tabs[2].getAttribute("aria-selected")).toBe("true");
    expect(tabs[2].querySelector("svg")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Close notes.md" }));
    expect(store().activeFile?.path).toBe("/w/src/main.py");
    fireEvent.click(screen.getByRole("tab", { name: "Shared context" }));
    expect(store().activeFile).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Fill the page" }));
    expect(store().maximized).toBe(true);
  });
});

describe("quick open", () => {
  it("lists recent files, searches the workspace, and opens a typed path as is", async () => {
    store().openFile({ agentId: 7, path: "/w/old.md" });
    transport.searchWorkspaceFiles.mockResolvedValue({ query: "rep", results: [{ path: "/w/reports/report.md", name: "report.md", relative: "reports/report.md" }], truncated: false });
    const closed: string[] = [];
    renderWithBrowser(<QuickOpen agentId={7} workspaceAgentId={7} onClose={() => closed.push("closed")} />, { wrapper: Wrapper });
    expect(screen.getByRole("option").textContent).toContain("old.md");

    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "rep" } });
    await waitFor(() => expect(screen.getByRole("option").textContent).toContain("report.md"));
    expect(transport.searchWorkspaceFiles).toHaveBeenCalledWith(7, "rep", 30, expect.anything());
    fireEvent.keyDown(input, { key: "Enter" });
    expect(store().activeFile).toEqual({ agentId: 7, path: "/w/reports/report.md" });
    expect(closed).toEqual(["closed"]);
  });

  it("offers a typed path first and opens it with Enter", () => {
    transport.searchWorkspaceFiles.mockResolvedValue({ query: "~/x/a.py", results: [], truncated: false });
    renderWithBrowser(<QuickOpen agentId={7} workspaceAgentId={7} onClose={() => undefined} />, { wrapper: Wrapper });
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "~/x/a.py" } });
    expect(screen.getAllByRole("option")[0].textContent).toContain("Open ~/x/a.py");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(store().activeFile).toEqual({ agentId: 7, path: "~/x/a.py" });
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
