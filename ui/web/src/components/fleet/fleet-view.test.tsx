import { QueryClient } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithProviders } from "@/test-support/render";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AGENTS_QUERY_KEY } from "@/lib/agents/use-agents";
import { USER_SETTING_DEFAULTS, type AgentRow, type NoticeItem } from "@/lib/contracts/types";
import {
  NOTICES_QUERY_KEY,
  NOTICES_RESOLVED_QUERY_KEY,
} from "@/lib/notifications/use-notices";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";

// meiva:note
// next/link needs no router in this isolated render — a plain anchor suffices.
// next/navigation: TaskGraph calls useRouter at its top level now (the graph
// mode's double-click → owner timeline path), so the layout tests need a stub.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: { children: ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

// meiva:note
// vitest+happy-dom has no localStorage by default — the view persists its mobile
// tab there, so provide a simple in-memory mock.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
const localStorageMock = (() => {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  };
})();
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock, writable: true });
Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
  configurable: true,
  value: vi.fn(),
});

let storedSettings: Record<string, unknown> = {};
const putSettingMock = vi.fn((key: string, value: unknown) => {
  storedSettings = { ...storedSettings, [key]: value };
  return Promise.resolve({ key, value, updated_at: "2026-06-06T00:00:00Z" });
});

// meiva:note
// What a test states about the queue: the three lists the feed hook renders. The rows are seeded
// into the notice caches in the shape the app stores them, so the real hook flattens them.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
interface FeedRows {
  open: NoticeItem[];
  awaiting: NoticeItem[];
  resolved: NoticeItem[];
}

const NO_NOTICES: FeedRows = { open: [], awaiting: [], resolved: [] };

import { BAR_HEIGHT_CLASS } from "@/lib/layout/layout";
import { FleetView } from "./fleet-view";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getNotices.mockImplementation(() =>
      Promise.resolve({ open: [], awaiting: [], resolved_page: [], next_cursor: null }));
  transport.getSettings.mockImplementation(() =>
      Promise.resolve({
        settings: Object.entries(storedSettings).map(([key, value]) => ({
          key,
          value,
          updated_at: "2026-06-06T00:00:00Z",
        })),
      }));
  transport.putSetting.mockImplementation((key: string, value: unknown) => putSettingMock(key, value));
});

beforeEach(() => {
  storedSettings = {};
  window.innerWidth = 1024;
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
  window.history.replaceState(null, "", "/fleet");
  window.innerWidth = 1024;
});

// meiva:note
// The test door for this file: the roster, the notice feed, the empty task registry and the empty
// page list enter through the caches the hooks read — the shape the app stores — and the transport
// answers the reads the panels still make, including the graph the Agents tab mounts. The settings
// cache holds what the test stated, so a persisted choice is a value, not a stubbed store.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function wrap(
  ui: ReactElement,
  { agents = [], notices = NO_NOTICES }: { agents?: AgentRow[]; notices?: FeedRows } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS, ...storedSettings });
  qc.setQueryData(AGENTS_QUERY_KEY, { agents, ancestors: [] });
  qc.setQueryData(NOTICES_QUERY_KEY, {
    open: notices.open,
    awaiting: notices.awaiting,
    resolved_page: [],
    next_cursor: null,
  });
  qc.setQueryData(NOTICES_RESOLVED_QUERY_KEY, {
    pages: [{ resolved_page: notices.resolved, next_cursor: null }],
    pageParams: [undefined],
  });
  for (const window of ["24h", "7d", "30d", "all"] as const) {
    qc.setQueryData(["tasks", "graph", window], { tasks: [] });
  }
  qc.setQueryData(["all-pages"], []);
  transport.getTasks.mockResolvedValue({ tasks: [] });
  transport.listAllPages.mockResolvedValue([]);
  transport.getFleetGraph.mockResolvedValue({
    nodes: [],
    edges: [],
    telemetry_stale: false,
    snapshot_at: null,
  });
  return renderWithProviders(ui, {
    queryClient: qc,
    settings: storedSettings,
    wallNow: () => Date.now(),
  });
}

function noticeItem(over: Partial<NoticeItem> & { id: number }): NoticeItem {
  return {
    agent_id: 1,
    agent_label: "worker",
    title: "Q",
    content: null,
    priority: "P3",
    require_response: true,
    blocking: false,
    created_at: "2026-06-06T00:00:00Z",
    resolved_at: null,
    resolution: null,
    reply: null,
    expire_at: "2026-06-07T00:00:00Z",
    ...over,
  };
}

function makeAgent(over: Partial<AgentRow> & { agent_id: number }): AgentRow {
  return {
    spawner: "user",
    harness: "meiva",
    fork_source_agent_id: null,
    status: "running",
    spawned_at: "2026-06-06T00:00:00Z",
    last_active_at: "2026-06-06T00:00:00Z", last_inbound_at: "2026-06-06T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    awaiting_response_count: 0, highest_notice_priority: null, unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    ...over,
  };
}

describe("FleetView (desktop)", () => {
  it("shows active/total counts and the graph surface", () => {
    const agents: AgentRow[] = [
      makeAgent({ agent_id: 1, label: "lead", status: "idling" }),
      makeAgent({ agent_id: 2, spawner: "agent:1", label: "worker" }),
      makeAgent({ agent_id: 3, spawner: "agent:1", status: "terminated" }),
    ];
    wrap(<FleetView />, { agents });
    // 2 alive (idling + running), 1 terminated
    expect(screen.getByText("2 active · 3 total")).toBeTruthy();
    expect(screen.getByLabelText("Agent graph legend")).toBeTruthy();
  });

  it("shows 0 active · 0 total when there are no agents", () => {
    const agents: AgentRow[] = [];
    wrap(<FleetView />, { agents });
    expect(screen.getByText("0 active · 0 total")).toBeTruthy();
  });

  it("root fills the pane width (flex-1) regardless of content (task #1066)", () => {
    // Regression: main is a row-flex landmark, so a root without flex-1
    // shrinks to its content max-width — clicking a short Inbox item
    // collapsed the whole fleet surface to the left ~60% of the viewport.
    const agents: AgentRow[] = [];
    const { container } = wrap(<FleetView />, { agents });
    const root = container.firstElementChild as HTMLElement;
    expect(root.classList.contains("flex-1")).toBe(true);
  });

  it("aligns the Agents/Tasks bar with the Inbox header (one shared height)", () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1 })];
    wrap(<FleetView />, { agents });
    // Both bars must pin the SAME explicit height — their content heights
    // differ, so equal padding would still leave the two borders off by a
    // couple of pixels.
    const heightClass = (el: Element) => [...el.classList].find((c) => /^h-\d/.test(c));
    const tabBar = screen.getByText("Agents").closest("div")!;
    const inboxHeader = screen.getByText("Inbox").closest("header")!;
    expect(heightClass(tabBar)).toBeDefined();
    expect(heightClass(tabBar)).toBe(heightClass(inboxHeader));
    expect(heightClass(tabBar)).toBe(BAR_HEIGHT_CLASS);
  });

  it("links the header back to the conversation", () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1 })];
    wrap(<FleetView />, { agents });
    const link = screen.getByLabelText("Back to conversation");
    expect(link.getAttribute("href")).toBe("/");
  });

  it("renders the unified Inbox panel (no Decisions/Reviews split)", () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1, unread_notice_count: 2 })];
    wrap(<FleetView />, { agents });
    expect(screen.getByText("Inbox")).toBeTruthy();
    expect(screen.queryByText("Decisions")).toBeNull();
    expect(screen.queryByText("Reviews")).toBeNull();
  });

  it("reads agent_id from the route and anchors that agent's notice", async () => {
    window.history.replaceState(null, "", "/fleet?agent_id=405");
    const agents: AgentRow[] = [
      makeAgent({ agent_id: 1, label: "lead" }),
      makeAgent({ agent_id: 405, label: "reviewer" }),
    ];
    const notices: FeedRows = {
      open: [],
      awaiting: [
        {
          id: 1405,
          agent_id: 405,
          agent_label: "reviewer",
          title: "Review result",
          content: null,
          priority: "P3",
          require_response: true,
          blocking: false,
          created_at: "2026-06-06T00:00:00Z",
          updated_at: null,
          resolved_at: null,
          resolution: null,
          reply: null,
          task_id: null,
          expire_at: "2026-06-07T00:00:00Z",
        },
      ],
      resolved: [],
    };

    wrap(<FleetView />, { agents, notices });

    const list = screen.getByRole("list", { name: /inbox queue/i });
    const row = within(list).getByText("Review result").closest('[data-testid="inbox-row"]');
    await waitFor(() => expect(row?.getAttribute("data-anchor-highlighted")).toBe("true"));
  });

  it("collapses the queue panel to a static handle and persists the choice", async () => {
    const agents: AgentRow[] = [
      makeAgent({
        agent_id: 1,
        awaiting_response_count: ([{ id: 1, title: "Q", content: null, priority: "P0", require_response: true, blocking: true, created_at: "2026-06-06T00:00:00Z" }]).length, highest_notice_priority: "P0",
      }),
    ];
    wrap(<FleetView />, { agents });
    fireEvent.click(screen.getByLabelText("Collapse queue"));

    // Queue gone; only the static handle remains — and it carries NO dynamic
    // signal (no badge/count), even though a P0 notice is pending.
    await waitFor(() => expect(screen.queryByText("Inbox")).toBeNull());
    const handle = screen.getByLabelText("Expand queue");
    expect(handle.textContent).toBe("Queue");
    expect(putSettingMock).toHaveBeenCalledWith("display.fleet_queue_collapsed", true);
  });

  it("expanding the collapsed queue restores the Inbox and persists", async () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1 })];
    wrap(<FleetView />, { agents });
    fireEvent.click(screen.getByLabelText("Collapse queue"));
    fireEvent.click(await screen.findByLabelText("Expand queue"));
    await waitFor(() => expect(screen.getByText("Inbox")).toBeTruthy());
    expect(putSettingMock).toHaveBeenLastCalledWith("display.fleet_queue_collapsed", false);
  });

  it("restores the collapsed state from the DB setting on mount", () => {
    storedSettings = { "display.fleet_queue_collapsed": true };
    const agents: AgentRow[] = [makeAgent({ agent_id: 1 })];
    wrap(<FleetView />, { agents });
    expect(screen.queryByText("Inbox")).toBeNull();
    expect(screen.getByLabelText("Expand queue")).toBeTruthy();
  });
});

describe("FleetView (mobile)", () => {
  beforeEach(() => {
    window.innerWidth = 375;
  });

  it("renders the Agents / Tasks / Inbox mobile tabs; Agents shows the graph", () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1, label: "lead", status: "running" })];
    wrap(<FleetView />, { agents });
    const tablist = screen.getByRole("tablist", { name: "Fleet sections" });
    expect(tablist.tagName).toBe("DIV");
    expect(screen.getByRole("tab", { name: /Agents/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Tasks/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Inbox/ })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /Decisions/ })).toBeNull();
    expect(screen.queryByRole("tab", { name: /Reviews/ })).toBeNull();
    expect(screen.getByLabelText("Agent graph legend")).toBeTruthy();
  });

  it("Inbox tab lands on the list; tapping a row opens detail; back returns to the list", () => {
    const agents: AgentRow[] = [makeAgent({ agent_id: 1, label: "lead" })];
    const notices: FeedRows = {
      open: [],
      awaiting: [
        {
          id: 101,
          agent_id: 1,
          agent_label: "lead",
          title: "What next?",
          content: null,
          priority: "P3",
          require_response: true,
          blocking: false,
          created_at: "2026-06-06T00:00:00Z",
          updated_at: null,
          resolved_at: null,
          resolution: null,
          reply: null,
          task_id: null,
          expire_at: "2026-06-07T00:00:00Z",
        },
      ],
      resolved: [],
    };
    wrap(<FleetView />, { agents, notices });
    fireEvent.click(screen.getByRole("tab", { name: /Inbox/ }));
    expect(screen.getByRole("list", { name: /inbox queue/i })).toBeTruthy();
    expect(screen.getByText("What next?")).toBeTruthy();
    expect(screen.queryByLabelText("Back to list")).toBeNull();
    fireEvent.click(screen.getByText("What next?"));
    expect(screen.getByLabelText("Back to list")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Back to list"));
    expect(screen.getByRole("list", { name: /inbox queue/i })).toBeTruthy();
    expect(screen.queryByLabelText("Back to list")).toBeNull();
  });

  // meiva:note
  // The tab badge and the panel must show one number. The badge used to roll up the
  // agent snapshot while the panel read the notice feed, so the same screen could
  // say "Inbox 4" over a list of two. The badge now reads the panel's own feed: the
  // agents below claim 1 decision + 3 unread FYI (4), but the queue only
  // includes the response-required notice. Legacy FYI must not inflate its badge.
  // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
  it("shows the Inbox badge from the same feed the panel renders", () => {
    const agents: AgentRow[] = [
      makeAgent({
        agent_id: 1,
        status: "running",
        awaiting_response_count: 1,
        highest_notice_priority: "P3",
        unread_notice_count: 3,
      }),
    ];
    const notices: FeedRows = {
      open: [noticeItem({ id: 2, require_response: false })],
      awaiting: [noticeItem({ id: 1 })],
      resolved: [],
    };
    wrap(<FleetView />, { agents, notices });
    expect(screen.getByRole("tab", { name: /Inbox 1/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Agents 1/ })).toBeTruthy();
  });

  it("persists mobile tab selection across remounts", () => {
    localStorage.setItem("meiva.fleet.mobileTab", "inbox");
    const agents: AgentRow[] = [makeAgent({ agent_id: 1, label: "lead", unread_notice_count: 1 })];
    wrap(<FleetView />, { agents });
    // Should restore to the Inbox tab.
    expect(screen.getByRole("tab", { name: /Inbox/, selected: true })).toBeTruthy();
  });

  it("deep link #inbox selects the Inbox tab on mobile", () => {
    window.location.hash = "#inbox";
    const agents: AgentRow[] = [makeAgent({ agent_id: 1, label: "lead" })];
    // meiva:note
    // The queue states its own row: the list only replaces the empty state when there is something
    // in it, so a test that wants the list says what the agent is waiting on.
    const notices: FeedRows = { open: [], awaiting: [noticeItem({ id: 1 })], resolved: [] };
    wrap(<FleetView />, { agents, notices });
    expect(screen.getByRole("tab", { name: /Inbox/, selected: true })).toBeTruthy();
    expect(screen.getByRole("list", { name: /inbox queue/i })).toBeTruthy();
    window.location.hash = "";
  });
});
