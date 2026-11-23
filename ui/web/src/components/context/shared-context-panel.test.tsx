import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, type RenderOptions, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { USER_SETTING_DEFAULTS } from "@/lib/contracts/types";
import { WallClockProvider } from "@/lib/transport/transport-context";
import type { useToUser } from "@/lib/context/use-to-user";

notifyManager.setScheduler((cb) => {
  cb();
});

// meiva:note
// The panel renders the real InspectorPanel directly. A test that cares whether Inspector mode
// is mounted reads `shared-inspector`, the panel's own wrapper, so nothing replaces the
// inspector module.
import { SharedContextPanel } from "./shared-context-panel";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";

import { resetTransport, transport } from "@/test-support/fake-api";
import { resetStreamHandlers, TestStreamProvider } from "@/test-support/fake-stream";
beforeEach(() => {
  resetTransport();
});

let qc: QueryClient;

// meiva:note
// Inspector reads time from the same clock owner as the rest of the page.
function PanelWrapper({ children }: { children: ReactNode }) {
  return (
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>
        <TestStreamProvider>{children}</TestStreamProvider>
      </QueryClientProvider>
    </WallClockProvider>
  );
}

function renderPanel(ui: ReactElement, options?: RenderOptions) {
  return renderWithBrowser(ui, { wrapper: PanelWrapper, ...options });
}

const snapshot = {
  get inspectorOpen() {
    const data = qc.getQueryData<Record<string, unknown>>(["user-settings"]);
    return data?.["display.inspector_open"] === true;
  },
  set inspectorOpen(open: boolean) {
    act(() => {
      qc.setQueryData<Record<string, unknown>>(["user-settings"], (old) => ({
        ...(old ?? USER_SETTING_DEFAULTS),
        "display.inspector_open": open,
      }));
    });
  },
};

type DocumentQuery = ReturnType<typeof useToUser>;

function documentQuery(): DocumentQuery {
  return {
    data: undefined,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
  } as unknown as DocumentQuery;
}

function renderPanelView({ mobile = false, onClose = vi.fn() }: {
  mobile?: boolean;
  onClose?: () => void;
} = {}) {
  return renderPanel(
    <SharedContextPanel
      agentId={7}
      document={documentQuery()}
      mobile={mobile}
      onClose={onClose}
    />,
  );
}

beforeEach(() => {
  useWorkspaceFileStore.getState().closeFile();
  window.history.replaceState(null, "", "/?agent_id=7");
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS, "display.inspector_open": false });
  transport.getSettings.mockImplementation(() => new Promise(() => undefined));
  transport.putSetting.mockImplementation(() => new Promise(() => undefined));
  transport.getToUser.mockImplementation(() => new Promise(() => undefined));
  transport.getOngoing.mockResolvedValue({ content: null, version: null, modified_at: null });
  transport.getAgentRoster.mockResolvedValue({ agents: [], ancestors: [] });
  transport.getAgentInspectLive.mockImplementation(() => new Promise(() => undefined));
  transport.getAgentInspectStatistics.mockImplementation(() => new Promise(() => undefined));
});

afterEach(() => {
  cleanup();
  resetStreamHandlers();
  vi.restoreAllMocks();
});

// meiva:note
// The panel's tabs are files (user ruling 2026-11-20): the document is the first one; the inspector
// stays behind its toggle, never a tab.
it("shows the document as the first file tab and keeps the inspector out of the tabs", () => {
  renderPanelView();
  expect(screen.getByText("No content")).toBeTruthy();
  expect(screen.queryByTestId("shared-inspector")).toBeNull();
  const tabs = screen.getAllByRole("tab");
  expect(tabs.map((tab) => tab.textContent)).toEqual(["Shared context", "Ongoing"]);
  expect(tabs[0].getAttribute("aria-selected")).toBe("true");
});

it.each([false, true])("shows Ongoing as a fixed tab and refreshes the Markdown (mobile=%s)", async (mobile) => {
  renderPanelView({ mobile });
  fireEvent.click(screen.getByRole("tab", { name: "Ongoing" }));
  expect(await screen.findByText("No ongoing items")).toBeTruthy();

  transport.getOngoing.mockResolvedValue({
    content: "## Doing\n- Review results",
    version: "review",
    modified_at: null,
  });
  await act(() => qc.invalidateQueries({ queryKey: ["ongoing", 7] }));
  expect(await screen.findByRole("heading", { name: "Doing" })).toBeTruthy();
  expect(screen.getByRole("img", { name: "Has ongoing items" })).toBeTruthy();
  expect(screen.getAllByRole("tab")).toHaveLength(2);
  if (!mobile) {
    fireEvent.click(screen.getByRole("button", { name: "Fill the page" }));
    expect(await screen.findByRole("heading", { name: "Doing" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back to the side panel" }));
  }

  fireEvent.click(screen.getByRole("tab", { name: "Shared context" }));
  expect(screen.getByText("No content")).toBeTruthy();
});

it("shows an error when the Ongoing document could not be read", async () => {
  transport.getOngoing.mockRejectedValue(new Error("Offline"));
  renderPanelView();
  fireEvent.click(screen.getByRole("tab", { name: "Ongoing" }));
  expect(await screen.findByText("Document unavailable. Try again later.")).toBeTruthy();
  expect(screen.queryByRole("img", { name: "Has ongoing items" })).toBeNull();
});

it("renders WorkspaceFileView when activeFile is set for this agent", async () => {
  transport.getWorkspaceFile.mockResolvedValue({
    path: "src/app.py",
    name: "app.py",
    extension: ".py",
    content: "x = 42\n",
    size: 7,
    binary: false,
    modified_at: "2026-11-07T12:00:00Z",
  });
  useWorkspaceFileStore.getState().openFile({ agentId: 7, path: "src/app.py", lineRange: "1" });

  renderPanelView();

  await waitFor(() => {
    expect(screen.getByTestId("workspace-file-view")).toBeTruthy();
    expect(screen.getByTestId("workspace-code-content")).toBeTruthy();
  });
});

it("swaps the body under a bar that stays, instead of laying a sheet over it", async () => {
  // meiva:note
  // The mode owns the panel: the panel bar (which names the mode and carries
  // the pressed magnifier that leaves it) stays as the one control surface, and
  // the body below it is replaced. An overlay that covered the bar left the
  // mode with no visible exit (user report 2026-10-20).
  snapshot.inspectorOpen = true;
  renderPanelView();
  expect(await screen.findByTestId("shared-inspector")).toBeTruthy();
  expect(screen.getByTestId("panel-bar").textContent).toContain("Inspector");
  expect(screen.getByTestId("shared-inspector")).toBeTruthy();
  expect(screen.queryByText("No content")).toBeNull();
  expect(screen.queryByTestId("question-bar")).toBeNull();
});

it("keeps questions out of the right panel and restores reading position across Inspector mode", async () => {
  const props = { agentId: 7, document: documentQuery(), mobile: false, onClose: vi.fn() };
  const { rerender } = renderPanel(<SharedContextPanel {...props} />);
  const body = screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  body.scrollTop = 420;
  fireEvent.click(screen.getByRole("button", { name: "Open inspector" }));
  snapshot.inspectorOpen = true;
  rerender(<SharedContextPanel {...props} />);
  expect(await screen.findByTestId("shared-inspector")).toBeTruthy();
  expect(screen.queryByTestId("question-region")).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  snapshot.inspectorOpen = false;
  rerender(<SharedContextPanel {...props} />);
  expect(readingArea().scrollTop).toBe(420);
  expect(screen.queryByTestId("question-region")).toBeNull();
});

function readingArea(): HTMLDivElement {
  return screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
}

it("starts a new agent's document at the top of the shared reading area", () => {
  const props = { agentId: 7, document: documentQuery(), mobile: false, onClose: vi.fn() };
  const { rerender } = renderPanel(<SharedContextPanel {...props} />);
  readingArea().scrollTop = 500;
  rerender(<SharedContextPanel {...props} agentId={8} />);
  // meiva:note
  // The document is keyed by agent, so switching agents rebuilds the reading
  // area: it starts at the top instead of carrying the previous agent's offset.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
  expect(readingArea().scrollTop).toBe(0);
});

function follows(first: Element, second: Element): boolean {
  return Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
}

it.each([false, true])("leaves no bottom region, and no gap, when there is no question (mobile=%s)", (mobile) => {
  qc.setQueryData(["user-messages", 7], {
    mode: "sdk",
    seen_id: 0,
    items: [
      {
        id: 1,
        agent_id: 7,
        mode: "sdk",
        source_key: "sdk:pub:1",
        content: "Published summary",
        suggestions: null,
        created_at: "2026-11-07T00:00:00Z",
      },
    ],
  });
  renderPanelView({ mobile });
  expect(screen.queryByTestId("question-region")).toBeNull();
  expect(screen.queryByRole("region", { name: "Ask" })).toBeNull();
  expect(screen.queryByText("Published summary")).toBeNull();
});

it.each([false, true])("pins the title bar outside the reading viewport, so a scroll moves only the content (inspector=%s)", async (inspector) => {
  // meiva:note
  // The bar belongs to the column, not to the reading area: both modes keep it
  // out of the scrolling element, so a scroll never carries it away. Shared
  // Context and Inspector each scroll only their own body.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
  snapshot.inspectorOpen = inspector;
  renderPanelView();
  if (inspector) expect(await screen.findByTestId("shared-inspector")).toBeTruthy();
  const bar = screen.getByTestId("panel-bar");
  const readingArea = screen.getByTestId(inspector ? "shared-inspector" : "shared-document");
  expect(readingArea.contains(bar)).toBe(false);
  expect(follows(bar, readingArea)).toBe(true);
  if (!inspector) {
    expect(readingArea.contains(screen.getByText("No content"))).toBe(true);
    expect(screen.queryByRole("region", { name: "Ask" })).toBeNull();
  }
});

it("closes the reading view from the mobile header", () => {
  const onClose = vi.fn();
  renderPanelView({ mobile: true, onClose });
  fireEvent.click(screen.getByRole("button", { name: "Back to messages" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("keeps the narrow Inspector toggle beside Back through mode changes", () => {
  const props = { agentId: 7, document: documentQuery(), mobile: true, onClose: vi.fn() };
  const { rerender } = renderPanel(<SharedContextPanel {...props} />);
  const back = screen.getByRole("button", { name: "Back to messages" });
  const toggle = screen.getByRole("button", { name: "Open inspector" });
  expect(toggle.parentElement).toBe(back.parentElement);
  expect(screen.queryByRole("button", { name: "Inspect" })).toBeNull();
  const body = readingArea();
  body.scrollTop = 180;
  fireEvent.click(toggle);
  snapshot.inspectorOpen = true;
  rerender(<SharedContextPanel {...props} />);
  const exit = screen.getByRole("button", { name: "Close inspector" });
  expect(exit).toBe(toggle);
  expect(exit.getAttribute("aria-pressed")).toBe("true");
  expect(exit.parentElement).toBe(back.parentElement);
  fireEvent.click(exit);
  snapshot.inspectorOpen = false;
  rerender(<SharedContextPanel {...props} />);
  // meiva:note
  // Narrow Inspector replaces the whole document section, so leaving it rebuilds
  // the reading area; the position comes back from the panel's own record.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
  expect(readingArea().scrollTop).toBe(180);
});

it("sizes the mobile full-screen sheet to --visual-viewport-height so an open keyboard never hides its reply composer", () => {
  renderPanelView({ mobile: true, onClose: vi.fn() });
  const sheet = screen.getByRole("complementary", { name: "To user" });
  expect(sheet.className).toContain("h-[var(--visual-viewport-height,100dvh)]");
  expect(sheet.className).not.toContain("inset-0");
});
