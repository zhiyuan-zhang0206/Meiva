import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, type RenderOptions, screen } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { USER_SETTING_DEFAULTS, type NoticeItem } from "@/lib/contracts/types";
import { WallClockProvider } from "@/lib/transport/transport-context";
import type { useToUser } from "@/lib/context/use-to-user";

notifyManager.setScheduler((cb) => {
  cb();
});

vi.mock("@/components/inspector/inspector-panel", () => ({
  InspectorPanel: () => <div data-testid="inspector-view" />,
}));

import { SharedContextPanel } from "./shared-context-panel";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});

let qc: QueryClient;

// meiva:note
// The panel renders the open-notice detail, which ages its labels against the page's clock; the
// wrapper mounts the app's own answer so this file keeps deciding nothing about time.
function PanelWrapper({ children }: { children: ReactNode }) {
  return (
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
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

function notice(): NoticeItem {
  return {
    id: 1,
    agent_id: 7,
    agent_label: null,
    title: "Needs review",
    content: null,
    priority: "P1",
    require_response: true,
    blocking: false,
    created_at: "2026-10-16T00:00:00Z",
    expire_at: "2026-10-17T00:00:00Z",
  };
}

function renderPanelView({ mobile = false, notices = [], onClose = vi.fn() }: {
  mobile?: boolean;
  notices?: NoticeItem[];
  onClose?: () => void;
} = {}) {
  return renderPanel(
    <SharedContextPanel
      agentId={7}
      document={documentQuery()}
      notices={notices}
      noticesFailed={false}
      mobile={mobile}
      onClose={onClose}
    />,
  );
}

beforeEach(() => {
  window.history.replaceState(null, "", "/?agent_id=7");
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS, "display.inspector_open": false });
  transport.getSettings.mockImplementation(() => new Promise(() => undefined));
  transport.putSetting.mockImplementation(() => new Promise(() => undefined));
  transport.getToUser.mockImplementation(() => new Promise(() => undefined));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("shows the document as the panel's body without a view switcher", () => {
  renderPanelView();
  expect(screen.getByText("No content")).toBeTruthy();
  expect(screen.queryByTestId("inspector-view")).toBeNull();
  expect(screen.queryByRole("tablist")).toBeNull();
});

it("swaps the body under a bar that stays, instead of laying a sheet over it", async () => {
  // meiva:note
  // The mode owns the panel: the document bar (which names the mode and carries
  // the pressed magnifier that leaves it) stays as the one control surface, and
  // the body below it is replaced. An overlay that covered the bar left the
  // mode with no visible exit (user report 2026-10-20).
  snapshot.inspectorOpen = true;
  renderPanelView();
  expect(await screen.findByTestId("inspector-view")).toBeTruthy();
  expect(screen.getByTestId("document-bar")).toBeTruthy();
  expect(screen.getByTestId("shared-inspector")).toBeTruthy();
  expect(screen.queryByText("No content")).toBeNull();
  expect(screen.queryByTestId("notice-bar")).toBeNull();
});

it("keeps the document in the reading area and the question out of every scroll viewport", () => {
  // meiva:note
  // The question is the column's bottom region: it never travels with the
  // reading area, so a long document scrolls under a card that stays put (user
  // report 2026-10-30).
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  renderPanelView({ notices: [notice()] });
  const body = screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  expect(body.contains(screen.getByText("No content"))).toBe(true);
  const question = screen.getByRole("region", { name: "Notification" });
  expect(body.contains(question)).toBe(false);
  expect(question.closest('[data-slot="scroll-area-viewport"]')).toBeNull();
  expect(screen.queryByText("Needs your reply")).toBeNull();
});

it("keeps the question at the bottom in Inspector mode and restores the reading position", async () => {
  // meiva:note
  // The bottom region is the column's, not the body's: switching to Inspector
  // replaces the reading area above it and leaves the pending question where the
  // reader left it, draft included (user 2026-10-30; until then the mode hid it
  // with the body it used to sit in).
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  const props = { agentId: 7, document: documentQuery(), notices: [notice()], noticesFailed: false, mobile: false, onClose: vi.fn() };
  const { rerender } = renderPanel(<SharedContextPanel {...props} />);
  const body = screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  const draft = screen.getByRole<HTMLTextAreaElement>("textbox", { name: /^Reply to notice:/ });
  fireEvent.change(draft, { target: { value: "Keep this answer" } });
  body.scrollTop = 420;
  fireEvent.click(screen.getByRole("button", { name: "Open inspector" }));
  snapshot.inspectorOpen = true;
  rerender(<SharedContextPanel {...props} />);
  expect(await screen.findByTestId("inspector-view")).toBeTruthy();
  expect(screen.getByRole("region", { name: "Notification" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: /^Reply to notice:/ })).toBe(draft);
  body.scrollTop = 0;
  snapshot.inspectorOpen = false;
  rerender(<SharedContextPanel {...props} />);
  expect(screen.getByRole("region", { name: "Notification" })).toBeTruthy();
  expect(body.scrollTop).toBe(420);
  expect(screen.getByRole("textbox", { name: /^Reply to notice:/ })).toBe(draft);
  expect(draft.value).toBe("Keep this answer");
});

function readingArea(): HTMLDivElement {
  return screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
}

it("starts a new agent or referenced document at the top of the shared reading area", () => {
  const props = { agentId: 7, document: documentQuery(), notices: [notice()], noticesFailed: false, mobile: false, onClose: vi.fn() };
  const { rerender, browser } = renderPanel(<SharedContextPanel {...props} />);
  const body = readingArea();
  body.scrollTop = 420;
  act(() => {
    browser.traverse(`http://fake.local/?agent_id=7&reference=${"a".repeat(64)}`);
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

it.each([false, true])("orders the column as title bar, then the reading area, then the question (mobile=%s)", (mobile) => {
  // meiva:note
  // Top to bottom: the bar heads the column, the reading area scrolls under it,
  // and the question is the column's own bottom region (user report 2026-10-30)
  // instead of the reading area's first block (2026-10-29). The order is the
  // same at every breakpoint.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  renderPanelView({ notices: [notice()], mobile });
  const bar = screen.getByTestId("document-bar");
  const document = screen.getByText("No content");
  const question = screen.getByRole("region", { name: "Notification" });
  expect(follows(bar, document)).toBe(true);
  expect(follows(document, question)).toBe(true);
  expect(screen.getAllByText("Shared context")).toHaveLength(1);
  expect(screen.queryByTestId("notice-bar")).toBeNull();
});

it.each([false, true])("pins the question as the column's last region in both modes (inspector=%s)", async (inspector) => {
  // meiva:note
  // The bottom region is the column's own last child, outside the reading
  // viewport and outside the Inspector body, so it is anchored under whatever
  // the column shows above it; without a question the region does not render at
  // all and takes no space.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  snapshot.inspectorOpen = inspector;
  renderPanelView({ notices: [notice()] });
  if (inspector) expect(await screen.findByTestId("inspector-view")).toBeTruthy();
  const column = screen.getByRole("complementary");
  const region = screen.getByTestId("notice-region");
  expect(column.lastElementChild).toBe(region);
  expect(follows(screen.getByTestId("shared-document"), region)).toBe(true);
  expect(region.closest('[data-slot="scroll-area-viewport"]')).toBeNull();
  if (inspector) expect(follows(screen.getByTestId("shared-inspector"), region)).toBe(true);
});

it.each([false, true])("leaves no bottom region, and no gap, when there is no question (mobile=%s)", (mobile) => {
  renderPanelView({ mobile });
  expect(screen.queryByTestId("notice-region")).toBeNull();
  expect(screen.queryByRole("region", { name: "Notification" })).toBeNull();
});

it.each([false, true])("pins the title bar outside the reading viewport, so a scroll moves only the content (inspector=%s)", async (inspector) => {
  // meiva:note
  // The bar belongs to the column, not to the reading area: both modes keep it
  // out of the scrolling element, so a scroll never carries it away — Shared
  // Context scrolls the question and document below it exactly as Inspector
  // scrolls its own body (user report 2026-10-29).
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  snapshot.inspectorOpen = inspector;
  renderPanelView({ notices: [notice()] });
  if (inspector) expect(await screen.findByTestId("inspector-view")).toBeTruthy();
  const bar = screen.getByTestId("document-bar");
  const readingArea = screen.getByTestId("shared-document");
  const viewport = readingArea.querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  expect(viewport.contains(bar)).toBe(false);
  expect(follows(bar, readingArea)).toBe(true);
  if (!inspector) {
    expect(viewport.contains(screen.getByText("No content"))).toBe(true);
    expect(viewport.contains(screen.getByRole("region", { name: "Notification" }))).toBe(false);
  }
});

it("closes the reading view from the mobile header", () => {
  const onClose = vi.fn();
  renderPanelView({ mobile: true, onClose });
  fireEvent.click(screen.getByRole("button", { name: "Back to messages" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("keeps the narrow Inspector toggle beside Back through mode changes", () => {
  const props = { agentId: 7, document: documentQuery(), notices: [notice()], noticesFailed: false, mobile: true, onClose: vi.fn() };
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
