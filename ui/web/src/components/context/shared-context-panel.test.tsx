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

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
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
  expect(screen.getByText("empty")).toBeTruthy();
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
  expect(screen.queryByText("empty")).toBeNull();
  expect(screen.queryByTestId("notice-bar")).toBeNull();
});

it("keeps the notice and document inside the shared reading area", () => {
  renderPanelView({ notices: [notice()] });
  const body = screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  expect(body.contains(screen.getByRole("region", { name: "notification" }))).toBe(true);
  expect(screen.queryByText("needsReply")).toBeNull();
});

it("hides the mounted question in Inspector mode and restores its reading position", async () => {
  const props = { agentId: 7, document: documentQuery(), notices: [notice()], noticesFailed: false, mobile: false, onClose: vi.fn() };
  const { rerender } = renderPanel(<SharedContextPanel {...props} />);
  const body = screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  const draft = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "replyTo" });
  fireEvent.change(draft, { target: { value: "Keep this answer" } });
  body.scrollTop = 420;
  fireEvent.click(screen.getByRole("button", { name: "openInspector" }));
  snapshot.inspectorOpen = true;
  rerender(<SharedContextPanel {...props} />);
  expect(await screen.findByTestId("inspector-view")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "notification" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "replyTo", hidden: true })).toBe(draft);
  body.scrollTop = 0;
  snapshot.inspectorOpen = false;
  rerender(<SharedContextPanel {...props} />);
  expect(screen.getByRole("region", { name: "notification" })).toBeTruthy();
  expect(body.scrollTop).toBe(420);
  expect(screen.getByRole("textbox", { name: "replyTo" })).toBe(draft);
  expect(draft.value).toBe("Keep this answer");
});

it("starts a new agent or referenced document at the top of the shared reading area", () => {
  const props = { agentId: 7, document: documentQuery(), notices: [notice()], noticesFailed: false, mobile: false, onClose: vi.fn() };
  const { rerender, browser } = renderPanel(<SharedContextPanel {...props} />);
  const body = screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  body.scrollTop = 420;
  act(() => {
    browser.traverse(`http://fake.local/?agent_id=7&reference=${"a".repeat(64)}`);
  });
  expect(body.scrollTop).toBe(0);
  body.scrollTop = 500;
  rerender(<SharedContextPanel {...props} agentId={8} />);
  expect(screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!).toBe(body);
  expect(body.scrollTop).toBe(0);
});

it.each([false, true])("keeps the question before and outside the document section, even without content (mobile=%s)", (mobile) => {
  renderPanelView({ notices: [notice()], mobile });
  const bar = screen.getByTestId("document-bar");
  const question = screen.getByRole("region", { name: "notification" });
  const document = screen.getByText("empty");
  expect(question.compareDocumentPosition(bar) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.getByRole("region", { name: "sharedContext" }).contains(question)).toBe(false);
  expect(screen.getAllByText("sharedContext")).toHaveLength(1);
  expect(question.compareDocumentPosition(document) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.queryByTestId("notice-bar")).toBeNull();
});

it("closes the reading view from the mobile header", () => {
  const onClose = vi.fn();
  renderPanelView({ mobile: true, onClose });
  fireEvent.click(screen.getByRole("button", { name: "backToMessages" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("keeps the narrow Inspector toggle beside Back through mode changes", () => {
  const props = { agentId: 7, document: documentQuery(), notices: [notice()], noticesFailed: false, mobile: true, onClose: vi.fn() };
  const { rerender } = renderPanel(<SharedContextPanel {...props} />);
  const back = screen.getByRole("button", { name: "backToMessages" });
  const toggle = screen.getByRole("button", { name: "openInspector" });
  expect(toggle.parentElement).toBe(back.parentElement);
  expect(screen.queryByRole("button", { name: "Inspect" })).toBeNull();
  const body = screen.getByTestId("shared-document").querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]')!;
  body.scrollTop = 180;
  fireEvent.click(toggle);
  snapshot.inspectorOpen = true;
  rerender(<SharedContextPanel {...props} />);
  const exit = screen.getByRole("button", { name: "closeInspector" });
  expect(exit).toBe(toggle);
  expect(exit.getAttribute("aria-pressed")).toBe("true");
  expect(exit.parentElement).toBe(back.parentElement);
  body.scrollTop = 0;
  fireEvent.click(exit);
  snapshot.inspectorOpen = false;
  rerender(<SharedContextPanel {...props} />);
  expect(body.scrollTop).toBe(180);
});
