import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import type { ReactNode } from "react";

import { preloadChatMarkdown } from "@/components/content/markdown";
import { WorkspaceFileView } from "./workspace-file-view";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithBrowser } from "@/test-support/render";
import { TestStreamProvider } from "@/test-support/fake-stream";
import { WallClockProvider } from "@/lib/transport/transport-context";

let qc: QueryClient;

function TestWrapper({ children }: { children: ReactNode }) {
  return (
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>
        <TestStreamProvider>{children}</TestStreamProvider>
      </QueryClientProvider>
    </WallClockProvider>
  );
}

describe("WorkspaceFileView", () => {
  beforeAll(async () => {
    await preloadChatMarkdown();
  });

  beforeEach(() => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    resetTransport();
    useWorkspaceFileStore.getState().closeFile();
  });

  afterEach(cleanup);

  it("renders source code with line numbers and highlights line range", async () => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "src/main.py",
      name: "main.py",
      extension: ".py",
      content: "line1\nline2\nline3\nline4\n",
      size: 24,
      binary: false,
      modified_at: "2026-11-07T12:00:00Z",
    });

    renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "src/main.py", lineRange: "2-3" }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      expect(screen.getByTestId("workspace-code-content")).toBeTruthy();
    });

    expect(screen.getByTestId("file-line-range-badge").textContent).toBe("#L2-3");

    const line2 = screen.getByText("line2").closest("[data-line-number]");
    expect(line2?.getAttribute("data-line-number")).toBe("2");
    expect(line2?.className).toContain("bg-primary/15");

    const line3 = screen.getByText("line3").closest("[data-line-number]");
    expect(line3?.getAttribute("data-line-number")).toBe("3");
    expect(line3?.className).toContain("bg-primary/15");

    const line1 = screen.getByText("line1").closest("[data-line-number]");
    expect(line1?.className).not.toContain("bg-primary/15");
  });

  it("renders markdown files through rich markdown", async () => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "docs/spec.md",
      name: "spec.md",
      extension: ".md",
      content: "# Specification\n\n**Important** rule\n",
      size: 32,
      binary: false,
      modified_at: "2026-11-07T12:00:00Z",
    });

    renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "docs/spec.md" }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Specification" })).toBeTruthy();
    });
    expect(screen.getByText("Important")).toBeTruthy();
  });

  it("hides the top refresh button while loading and shows spinner only in content", () => {
    transport.getWorkspaceFile.mockImplementation(() => new Promise<never>(() => undefined));

    renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "assets/logo.png" }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    const bar = screen.getByTestId("workspace-file-bar");
    expect(within(bar).queryByRole("button", { name: "Refresh" })).toBeNull();
    expect(bar.querySelector(".animate-spin")).toBeNull();
    const spinner = screen.getByRole("status", { name: "Loading…" });
    expect(bar.contains(spinner)).toBe(false);
  });

  it("renders images through ZoomableImage using raw media url without duplicate footer metadata or spinning refresh", async () => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "assets/logo.png",
      name: "logo.png",
      extension: ".png",
      content: "",
      size: 100,
      binary: true,
      modified_at: "2026-11-07T12:00:00Z",
    });

    renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "assets/logo.png" }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      const img = screen.getByRole("img", { name: "logo.png" });
      expect(img.getAttribute("src")).toContain("/api/agents/1/files/raw?path=assets%2Flogo.png");
    });

    const bar = screen.getByTestId("workspace-file-bar");
    expect(within(bar).getByText("logo.png")).toBeTruthy();
    expect(within(bar).getByText("(100 B)")).toBeTruthy();
    expect(screen.getAllByText(/logo\.png/)).toHaveLength(1);
    expect(screen.queryByText("logo.png (100 B)")).toBeNull();

    const refreshBtn = within(bar).getByRole<HTMLButtonElement>("button", { name: "Refresh" });
    expect(refreshBtn.querySelector(".animate-spin")).toBeNull();
    transport.getWorkspaceFile.mockImplementation(() => new Promise<never>(() => undefined));
    fireEvent.click(refreshBtn);
    await waitFor(() => expect(refreshBtn.disabled).toBe(true));
    expect(refreshBtn.querySelector(".animate-spin")).toBeNull();
  });

  it("renders video files using video player with raw media url without duplicate footer metadata", async () => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "media/demo.mp4",
      name: "demo.mp4",
      extension: ".mp4",
      content: "",
      size: 10240,
      binary: true,
      modified_at: "2026-11-07T12:00:00Z",
    });

    renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "media/demo.mp4" }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      const video = screen.getByTestId("workspace-video-player");
      expect(video.getAttribute("src")).toContain("/api/agents/1/files/raw?path=media%2Fdemo.mp4");
    });

    const bar = screen.getByTestId("workspace-file-bar");
    expect(within(bar).getByText("demo.mp4")).toBeTruthy();
    expect(within(bar).getByText("(10.0 KB)")).toBeTruthy();
    expect(screen.getAllByText(/demo\.mp4/)).toHaveLength(1);
    expect(screen.queryByText("demo.mp4 (10.0 KB)")).toBeNull();
  });

  it.each([".html", ".htm"])("renders %s files as source code", async (extension) => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "research/learning-library.html",
      name: "learning-library.html",
      extension,
      content: "<!doctype html><html><body><h1>Interactive Report</h1></body></html>",
      size: 68,
      binary: false,
      modified_at: "2026-11-09T12:00:00Z",
    });

    const { container } = renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "research/learning-library.html" }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    const code = await screen.findByTestId("workspace-code-content");
    expect(code.textContent).toContain("<h1>Interactive Report</h1>");
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("allows returning to to_user.md or going back in history", () => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "test.py",
      name: "test.py",
      extension: ".py",
      content: "x = 1\n",
      size: 6,
      binary: false,
      modified_at: "2026-11-07T12:00:00Z",
    });

    const store = useWorkspaceFileStore.getState();
    store.openFile({ agentId: 1, path: "file1.py" });
    store.openFile({ agentId: 1, path: "file2.py" });

    renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "file2.py" }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    const homeBtn = screen.getByTitle("Return to to_user.md");
    fireEvent.click(homeBtn);
    expect(useWorkspaceFileStore.getState().activeFile).toBeNull();
  });

  it("routes explicit cross-agent workspace paths to the owning agent for content and raw media", async () => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "deliver/user-positions-before-after.png",
      name: "user-positions-before-after.png",
      extension: ".png",
      content: "",
      size: 695326,
      binary: true,
      modified_at: "2026-11-18T03:08:00Z",
    });

    renderWithBrowser(
      <WorkspaceFileView
        agentId={7516}
        target={{
          agentId: 7516,
          path: "/Users/zhiyuan-output/.meiva/workspaces/7765/deliver/user-positions-before-after.png",
        }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      const img = screen.getByRole("img", { name: "user-positions-before-after.png" });
      expect(img.getAttribute("src")).toContain(
        "/api/agents/7765/files/raw?path=deliver%2Fuser-positions-before-after.png",
      );
    });
    expect(transport.getWorkspaceFile).toHaveBeenCalledWith(
      7765,
      "deliver/user-positions-before-after.png",
      expect.anything(),
    );
  });

  it("resolves relative images and links inside a cross-agent subdirectory Markdown file against that owner and directory", async () => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "deliver/REPORT.md",
      name: "REPORT.md",
      extension: ".md",
      content: [
        "# Position Report",
        "",
        "![Comparison](user-positions-before-after.png)",
        "",
        "[Open chart](user-positions-before-after.png)",
        "",
        "[Parent notes](../notes.md#L12)",
      ].join("\n"),
      size: 128,
      binary: false,
      modified_at: "2026-11-18T03:08:00Z",
    });

    renderWithBrowser(
      <WorkspaceFileView
        agentId={7516}
        target={{
          agentId: 7516,
          path: "/Users/zhiyuan-output/.meiva/workspaces/7765/deliver/REPORT.md",
        }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Position Report" })).toBeTruthy();
    });
    expect(transport.getWorkspaceFile).toHaveBeenCalledWith(
      7765,
      "deliver/REPORT.md",
      expect.anything(),
    );

    const img = screen.getByRole("img", { name: "Comparison" });
    expect(img.getAttribute("src")).toContain(
      "/api/agents/7765/files/raw?path=deliver%2Fuser-positions-before-after.png",
    );

    fireEvent.click(screen.getByRole("link", { name: "Open chart" }));
    expect(useWorkspaceFileStore.getState().activeFile).toEqual({
      agentId: 7516,
      path: "/Users/zhiyuan-output/.meiva/workspaces/7765/deliver/user-positions-before-after.png",
      lineRange: undefined,
    });

    fireEvent.click(screen.getByRole("link", { name: "Parent notes" }));
    expect(useWorkspaceFileStore.getState().activeFile).toEqual({
      agentId: 7516,
      path: "/Users/zhiyuan-output/.meiva/workspaces/7765/notes.md",
      lineRange: "12",
    });
  });
});
