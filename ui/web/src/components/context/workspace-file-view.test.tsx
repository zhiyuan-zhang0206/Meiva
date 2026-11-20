import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
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
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      expect(screen.getByTestId("workspace-code-content")).toBeTruthy();
    });


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
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Specification" })).toBeTruthy();
    });
    expect(screen.getByText("Important")).toBeTruthy();
  });

  it("shows only the content spinner while loading, with no bar or refresh of its own", () => {
    transport.getWorkspaceFile.mockImplementation(() => new Promise<never>(() => undefined));

    renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "assets/logo.png" }}
      />,
      { wrapper: TestWrapper },
    );

    // meiva:note
    // The view has no bar of its own (the panel bar and path bar own the controls); a load shows
    // only the content spinner.
    expect(screen.queryByTestId("workspace-file-bar")).toBeNull();
    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
    expect(screen.getByRole("status", { name: "Loading…" })).toBeTruthy();
  });

  it("renders images through ZoomableImage using the raw media url", async () => {
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
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      const img = screen.getByRole("img", { name: "logo.png" });
      expect(img.getAttribute("src")).toContain("/api/agents/1/files/raw?path=assets%2Flogo.png");
    });

    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
  });

  it("renders video files in the video player from the raw media url", async () => {
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
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      const video = screen.getByTestId("workspace-video-player");
      expect(video.getAttribute("src")).toContain("/api/agents/1/files/raw?path=media%2Fdemo.mp4");
    });

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
      />,
      { wrapper: TestWrapper },
    );

    const code = await screen.findByTestId("workspace-code-content");
    expect(code.textContent).toContain("<h1>Interactive Report</h1>");
    expect(container.querySelector("iframe")).toBeNull();
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
