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
  transport.getAgentRoster.mockResolvedValue({ agents: [], ancestors: [] });
  transport.getAgentInspectLive.mockImplementation(() => new Promise(() => undefined));
  transport.getAgentInspectStatistics.mockImplementation(() => new Promise(() => undefined));
  transport.getAgentInspectWidgets.mockImplementation(() => new Promise(() => undefined));
});

afterEach(() => {
  cleanup();
  resetStreamHandlers();
  vi.restoreAllMocks();
});

it("shows the document as the panel's body without a view switcher", () => {
  renderPanelView();
  expect(screen.getByText("No content")).toBeTruthy();
  expect(screen.queryByTestId("shared-inspector")).toBeNull();
  expect(screen.queryByRole("tablist")).toBeNull();
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
  // The mode owns the panel: the document bar (which names the mode and carries
  // the pressed magnifier that leaves it) stays as the one control surface, and
  // the body below it is replaced. An overlay that covered the bar left the
  // mode with no visible exit (user report 2026-10-20).
  snapshot.inspectorOpen = true;
  renderPanelView();
  expect(await screen.findByTestId("shared-inspector")).toBeTruthy();
  expect(screen.getByTestId("document-bar")).toBeTruthy();
  expect(screen.getByTestId("shared-inspector")).toBeTruthy();
  expect(screen.queryByText("No content")).toBeNull();
  expect(screen.queryByTestId("notice-bar")).toBeNull();
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
  expect(screen.queryByTestId("notice-region")).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  body.scrollTop = 0;
  snapshot.inspectorOpen = false;
  rerender(<SharedContextPanel {...props} />);
  expect(body.scrollTop).toBe(420);
  expect(screen.queryByTestId("notice-region")).toBeNull();
});

function readingArea(): HTMLDivElement {
  return screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
}

it("starts a new agent or referenced document at the top of the shared reading area", () => {
  const props = { agentId: 7, document: documentQuery(), mobile: false, onClose: vi.fn() };
  const { rerender, browser } = renderPanel(<SharedContextPanel {...props} />);
  const body = readingArea();
  body.scrollTop = 420;
  act(() => {
    browser.traverse("http://fake.local/?agent_id=7&file=reports%2Fdetails.md");
  });
  expect(body.scrollTop).toBe(0);
  body.scrollTop = 500;
  rerender(<SharedContextPanel {...props} agentId={8} />);
  // meiva:note
  // The document is keyed by agent, so switching agents rebuilds the reading
  // area: it starts at the top instead of carrying the previous agent's offset.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  expect(readingArea().scrollTop).toBe(0);
});

function follows(first: Element, second: Element): boolean {
  return Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
}

it.each([false, true])("leaves no bottom region, and no gap, when there is no question (mobile=%s)", (mobile) => {
  renderPanelView({ mobile });
  expect(screen.queryByTestId("notice-region")).toBeNull();
  expect(screen.queryByRole("region", { name: "Ask" })).toBeNull();
});

it.each([false, true])("pins the title bar outside the reading viewport, so a scroll moves only the content (inspector=%s)", async (inspector) => {
  // meiva:note
  // The bar belongs to the column, not to the reading area: both modes keep it
  // out of the scrolling element, so a scroll never carries it away — Shared
  // Context scrolls the question and document below it exactly as Inspector
  // scrolls its own body (user report 2026-10-29).
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  snapshot.inspectorOpen = inspector;
  renderPanelView();
  if (inspector) expect(await screen.findByTestId("shared-inspector")).toBeTruthy();
  const bar = screen.getByTestId("document-bar");
  const readingArea = screen.getByTestId("shared-document");
  const viewport = readingArea.querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  expect(viewport.contains(bar)).toBe(false);
  expect(follows(bar, readingArea)).toBe(true);
  if (!inspector) {
    expect(viewport.contains(screen.getByText("No content"))).toBe(true);
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
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  expect(readingArea().scrollTop).toBe(180);
});

it("sizes the mobile full-screen sheet to --visual-viewport-height so an open keyboard never hides its reply composer", () => {
  renderPanelView({ mobile: true, onClose: vi.fn() });
  const sheet = screen.getByRole("complementary", { name: "To user" });
  expect(sheet.className).toContain("h-[var(--visual-viewport-height,100dvh)]");
  expect(sheet.className).not.toContain("inset-0");
});
