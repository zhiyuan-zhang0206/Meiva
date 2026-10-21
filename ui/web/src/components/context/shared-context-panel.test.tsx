import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const snapshot = vi.hoisted(() => ({ inspectorOpen: false }));

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a>,
}));
vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));
vi.mock("@/lib/inspector/inspector-panel-store", () => ({
  useInspectorOpen: () => ({ open: snapshot.inspectorOpen, toggle: vi.fn() }),
}));
vi.mock("./to-user-document", () => ({
  ToUserDocumentView: function Document({ bodyHidden, onInspect, onNavigate }: { bodyHidden?: boolean; onInspect: () => void; onNavigate: () => void }) {
    useEffect(onNavigate, [onNavigate]);
    return (
    <div>
      <div data-testid="document-bar" />
      <button onClick={onInspect}>Inspect</button>
      <button onClick={onNavigate}>Navigate</button>
      {bodyHidden ? null : <div data-testid="document-view" />}
    </div>
    );
  },
}));
vi.mock("@/components/inspector/inspector-panel", () => ({
  InspectorPanel: () => <div data-testid="inspector-view" />,
}));
vi.mock("@/components/notifications/open-notice-detail", () => ({
  OpenNoticeDetail: () => <div data-testid="notice-detail" />,
}));
vi.mock("@/lib/notifications/use-notices", () => ({ dropOpenNotices: vi.fn() }));
vi.mock("@/lib/inspector/inspector-widgets", () => ({ fleetNoticeHref: (id: number) => `/fleet?notice=${id}` }));

import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS } from "@/lib/layout/layout";
import type { NoticeItem } from "@/lib/contracts/types";
import type { useToUser } from "@/lib/context/use-to-user";
import { SharedContextPanel } from "./shared-context-panel";

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

function renderPanel({ mobile = false, notices = [], onClose = vi.fn() }: {
  mobile?: boolean;
  notices?: NoticeItem[];
  onClose?: () => void;
} = {}) {
  return render(
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
  snapshot.inspectorOpen = false;
});

afterEach(cleanup);

it("shows the document as the panel's body without a view switcher", () => {
  renderPanel();
  expect(screen.getByTestId("document-view")).toBeTruthy();
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
  renderPanel();
  expect(await screen.findByTestId("inspector-view")).toBeTruthy();
  expect(screen.getByTestId("document-bar")).toBeTruthy();
  expect(screen.getByTestId("shared-inspector")).toBeTruthy();
  expect(screen.queryByTestId("document-view")).toBeNull();
  expect(screen.queryByTestId("notice-bar")).toBeNull();
});

it("keeps the notice area inside the document body", () => {
  renderPanel({ notices: [notice()] });
  const body = screen.getByTestId("shared-document");
  expect(body.contains(screen.getByTestId("notice-detail"))).toBe(true);
  expect(screen.getByText("needsReply")).toBeTruthy();
});

it("hides the mounted question in Inspector mode and restores its reading position", async () => {
  const props = { agentId: 7, document: documentQuery(), notices: [notice()], noticesFailed: false, mobile: false, onClose: vi.fn() };
  const { rerender } = render(<SharedContextPanel {...props} />);
  const body = screen.getByTestId("shared-document");
  const detail = screen.getByTestId("notice-detail");
  body.scrollTop = 420;
  fireEvent.click(screen.getByRole("button", { name: "Inspect" }));
  snapshot.inspectorOpen = true;
  rerender(<SharedContextPanel {...props} />);
  expect(await screen.findByTestId("inspector-view")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "notification" })).toBeNull();
  expect(screen.getByTestId("notice-detail")).toBe(detail);
  body.scrollTop = 0;
  snapshot.inspectorOpen = false;
  rerender(<SharedContextPanel {...props} />);
  expect(screen.getByRole("region", { name: "notification" })).toBeTruthy();
  expect(body.scrollTop).toBe(420);
});

it("starts a new agent or referenced document at the top of the shared reading area", () => {
  const props = { agentId: 7, document: documentQuery(), notices: [notice()], noticesFailed: false, mobile: false, onClose: vi.fn() };
  const { rerender } = render(<SharedContextPanel {...props} />);
  const body = screen.getByTestId("shared-document");
  body.scrollTop = 420;
  fireEvent.click(screen.getByRole("button", { name: "Navigate" }));
  expect(body.scrollTop).toBe(0);
  body.scrollTop = 500;
  rerender(<SharedContextPanel {...props} agentId={8} />);
  expect(screen.getByTestId("shared-document")).toBe(body);
  expect(body.scrollTop).toBe(0);
});

it("uses the shared column title bar for the notice header", () => {
  renderPanel({ notices: [notice()] });
  const tokens = screen.getByTestId("notice-bar").className.split(/\s+/);
  expect(tokens).toEqual(expect.arrayContaining(["relative", BAR_HEIGHT_CLASS]));
  expect(tokens).toEqual(expect.arrayContaining(BAR_DIVIDER_CLASS.split(" ")));
  expect(tokens).not.toContain("border-b");
  expect(screen.getByRole("region", { name: "notification" }).className.split(/\s+/)).not.toContain("border-b");
});

it("closes the reading view from the mobile header", () => {
  const onClose = vi.fn();
  renderPanel({ mobile: true, onClose });
  fireEvent.click(screen.getByRole("button", { name: "backToMessages" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});
