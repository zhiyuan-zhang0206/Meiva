import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ReactNode } from "react";

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

  it("renders images through ZoomableImage using raw media url", async () => {
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
  });

  it("renders video files using video player with raw media url", async () => {
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
  });

  it("renders audio files using audio player with raw media url", async () => {
    transport.getWorkspaceFile.mockResolvedValue({
      path: "media/sound.mp3",
      name: "sound.mp3",
      extension: ".mp3",
      content: "",
      size: 4096,
      binary: true,
      modified_at: "2026-11-07T12:00:00Z",
    });

    renderWithBrowser(
      <WorkspaceFileView
        agentId={1}
        target={{ agentId: 1, path: "media/sound.mp3" }}
        mobile={false}
      />,
      { wrapper: TestWrapper },
    );

    await waitFor(() => {
      const audio = screen.getByTestId("workspace-audio-player");
      expect(audio.getAttribute("src")).toContain("/api/agents/1/files/raw?path=media%2Fsound.mp3");
    });
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
});
