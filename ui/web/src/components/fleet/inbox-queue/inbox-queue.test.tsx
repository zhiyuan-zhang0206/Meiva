import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import type { ComponentProps } from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import type { AgentRow, NoticeItem, OpenNotice, PageRow, TaskSummaryRow } from "@/lib/contracts/types";
import type { NoticesFeed } from "@/lib/notifications/use-notices";

const resolveNotice = vi.fn<
  (agentId: number, noticeId: number, body: { action: string; reply?: string }) => Promise<{ status: string }>
>();

const inboxFeedMock = vi.fn<() => NoticesFeed>();
const dropOpenNotices = vi.fn<(queryClient: unknown, noticeIds: number[]) => void>();
vi.mock("@/lib/notifications/use-notices", () => ({
  useNotices: () => inboxFeedMock(),
  dropOpenNotices: (qc: unknown, ids: number[]) => dropOpenNotices(qc, ids),
}));

const useTasksMock = vi.fn<(window?: string, fields?: string) => { tasks: TaskSummaryRow[]; loading: boolean; error: boolean }>(
  () => ({ tasks: [], loading: false, error: false }),
);
vi.mock("@/lib/notifications/use-tasks", () => ({
  useTasks: (window?: string, fields?: string) => useTasksMock(window, fields),
}));

const useAllPagesMock = vi.fn<() => PageRow[]>(() => []);
vi.mock("@/lib/agents/use-all-pages", () => ({ useAllPages: () => useAllPagesMock() }));

import { InboxQueue } from "../inbox-queue";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.resolveNotice.mockImplementation((a: number, n: number, b: { action: string; reply?: string }) => resolveNotice(a, n, b));
  transport.getSettings.mockImplementation(() => Promise.resolve({ settings: [] }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  inboxFeedMock.mockReturnValue(emptyFeed());
  useTasksMock.mockReturnValue({ tasks: [], loading: false, error: false });
  useAllPagesMock.mockReturnValue([]);
});

function emptyFeed(over: Partial<NoticesFeed> = {}): NoticesFeed {
  return {
    open: over.open ?? [],
    awaiting: over.awaiting ?? [],
    resolved: over.resolved ?? [],
    fetchNextPage: over.fetchNextPage ?? vi.fn(),
    hasNextPage: over.hasNextPage ?? false,
    isFetchingNextPage: over.isFetchingNextPage ?? false,
    error: over.error ?? false,
    resolvedError: over.resolvedError ?? false,
    isLoading: over.isLoading ?? false,
  };
}
function setFeed(over: Partial<NoticesFeed> = {}) {
  inboxFeedMock.mockReturnValue(emptyFeed(over));
}

function n(over: Partial<NoticeItem> & { id: number; title: string }): NoticeItem {
  return {
    id: over.id,
    agent_id: over.agent_id ?? 7,
    agent_label: over.agent_label ?? "worker",
    title: over.title,
    content: over.content ?? null,
    priority: over.priority ?? "P2",
    require_response: over.require_response ?? true,
    blocking: over.blocking ?? false,
    created_at: over.created_at ?? "2026-06-14T00:00:00Z",
    updated_at: over.updated_at ?? null,
    resolved_at: null,
    resolution: null,
    reply: null,
    task_id: over.task_id ?? null,
    expire_at: over.expire_at ?? "2026-06-15T00:00:00Z",
  };
}

function ni(over: Partial<NoticeItem> & { id: number; title: string }): NoticeItem {
  return {
    id: over.id,
    agent_id: over.agent_id ?? 5,
    agent_label: over.agent_label ?? "worker",
    title: over.title,
    content: over.content ?? null,
    priority: over.priority ?? "P2",
    require_response: over.require_response ?? false,
    blocking: over.blocking ?? false,
    created_at: over.created_at ?? "2026-06-17T00:00:00Z",
    resolved_at: over.resolved_at ?? null,
    resolution: over.resolution ?? null,
    reply: over.reply ?? null,
    expire_at: over.expire_at ?? "2026-06-18T00:00:00Z",
  };
}

function agent(over: {
  agent_id: number;
  label?: string;
  status?: AgentRow["status"];
  notices_awaiting_response?: OpenNotice[];
}): AgentRow {
  return {
    agent_id: over.agent_id,
    harness: "meiva",
    spawner: "user",
    fork_source_agent_id: null,
    status: over.status ?? "idling",
    spawned_at: "2026-06-14T00:00:00Z",
    last_active_at: "2026-06-14T00:00:00Z", last_inbound_at: "2026-06-14T00:00:00Z",
    label: over.label ?? null,
    machine: "test",
    supports_vision: true,
    awaiting_response_count: (over.notices_awaiting_response ?? []).length, highest_notice_priority: (over.notices_awaiting_response ?? [])[0]?.priority ?? null,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
  };
}

function renderQueue(
  agents: AgentRow[],
  props: Omit<ComponentProps<typeof InboxQueue>, "agents"> = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return renderWithBrowser(
    <QueryClientProvider client={qc}>
      <InboxQueue agents={agents} {...props} />
    </QueryClientProvider>,
  );
}

function listTitles(): string[] {
  const list = screen.getByRole("list", { name: /inbox queue/i });
  return within(list)
    .getAllByRole("button")
    .map((b) => b.querySelector('[data-testid="notice-title"]')?.textContent ?? "")
    .filter((t) => t !== "");
}

describe("InboxQueue — open stream", () => {
  it("formats queue titles without putting active links inside selection buttons", async () => {
    setFeed({ awaiting: [n({ id: 10, title: "**Decide** [scope](https://example.com/scope)" })] });
    renderQueue([agent({ agent_id: 7 })]);
    const row = screen.getByTestId("notice-title");
    await waitFor(() => expect(row.querySelector("strong")?.textContent).toBe("Decide"));
    expect(row.querySelector("a, div, p")).toBeNull();
    expect(screen.getByRole("link", { name: "scope" }).getAttribute("href")).toBe("https://example.com/scope");
  });

  it("shows an empty state when nothing is open and nothing resolved", () => {
    setFeed();
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText(/Nothing needs your attention/)).not.toBeNull();
  });

  it("excludes legacy FYI entries from the response queue", () => {
    setFeed({
      open: [ni({ id: 20, title: "fyi-p0", priority: "P0" }), ni({ id: 21, title: "fyi-p3", priority: "P3" })],
      awaiting: [n({ id: 10, title: "dec-p2", priority: "P2" })],
    });
    renderQueue([agent({ agent_id: 1, label: "a" })]);
    expect(listTitles()).toEqual(["dec-p2"]);
  });

  it("labels every response-required row", () => {
    setFeed({
      open: [ni({ id: 40, title: "just-fyi", priority: "P2" })],
      awaiting: [
        n({ id: 41, title: "needs-answer", priority: "P2" }),
        n({ id: 42, title: "stalled", priority: "P2", blocking: true }),
      ],
    });
    renderQueue([agent({ agent_id: 1, label: "a" })]);
    const list = screen.getByRole("list", { name: /inbox queue/i });
    const rungOf = (title: string) =>
      within(list)
        .getByText(title)
        .closest('[data-testid="inbox-row"]')
        ?.querySelector('[data-testid="notice-kind"]')?.textContent;
    expect(within(list).queryByText("just-fyi")).toBeNull();
    expect(rungOf("needs-answer")).toBe("Decision");
    expect(rungOf("stalled")).toBe("Decision");
  });

  it("breaks priority ties by blocking, then oldest", () => {
    setFeed({
      awaiting: [
        n({ id: 30, title: "t-newer", priority: "P2", created_at: "2026-06-14T02:00:00Z" }),
        n({ id: 31, title: "t-older", priority: "P2", created_at: "2026-06-14T01:00:00Z" }),
        n({ id: 32, title: "t-block", priority: "P2", blocking: true, created_at: "2026-06-14T03:00:00Z" }),
      ],
    });
    renderQueue([agent({ agent_id: 1, label: "a" })]);
    expect(listTitles()).toEqual(["t-block", "t-older", "t-newer"]);
  });
});

describe("InboxQueue — route anchor", () => {
  it("scrolls to and briefly highlights the matching notice without reordering", async () => {
    vi.useFakeTimers();
    const raf = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback) => {
        callback(0);
        return 1;
      });
    try {
      setFeed({
        awaiting: [
          n({ id: 1, agent_id: 1, title: "first", priority: "P0" }),
          n({ id: 2, agent_id: 405, title: "target", priority: "P3" }),
        ],
      });
      const { browser } = renderQueue(
        [agent({ agent_id: 1 }), agent({ agent_id: 405 })],
        { anchorAgentId: 405 },
      );

      expect(listTitles()).toEqual(["first", "target"]);
      const target = screen.getByText("target").closest('[data-testid="inbox-row"]');
      expect(target?.getAttribute("data-anchor-highlighted")).toBe("true");
      expect(target?.className).toContain("bg-primary/10");
      expect(browser.actions.scrolled).toEqual([{ target, block: "nearest" }]);

      await act(() => vi.advanceTimersByTime(1_600));
      expect(target?.getAttribute("data-anchor-highlighted")).toBeNull();
      expect(listTitles()).toEqual(["first", "target"]);
    } finally {
      raf.mockRestore();
      vi.useRealTimers();
    }
  });

  it("keeps the default position when that agent has no notice", () => {
    setFeed({
      awaiting: [
        n({ id: 1, agent_id: 1, title: "first", priority: "P0" }),
        n({ id: 2, agent_id: 2, title: "second", priority: "P3" }),
      ],
    });
    const { browser } = renderQueue(
      [agent({ agent_id: 1 }), agent({ agent_id: 2 })],
      { anchorAgentId: 405 },
    );

    expect(listTitles()).toEqual(["first", "second"]);
    expect(screen.getByRole("heading", { name: "first" })).toBeTruthy();
    expect(document.querySelector('[data-anchor-highlighted="true"]')).toBeNull();
    expect(browser.actions.scrolled).toEqual([]);
  });
});

describe("InboxQueue — decision (require_response) action surface", () => {
  it("auto-selects the top notice; submits an answer via the Send button", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ awaiting: [n({ id: 99, title: "Send it?", content: "A) yes\nB) no", priority: "P0" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    expect(screen.getByText(/A\) yes/)).not.toBeNull();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "A" } });
    fireEvent.click(screen.getByLabelText("Send answer"));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 99, { action: "answer", reply: "A" }));
  });

  it("Enter submits; Shift+Enter does not; IME composition does not", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "yes" } });

    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(resolveNotice).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(resolveNotice).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 99, { action: "answer", reply: "yes" }));
  });

  it("does not submit a blank answer", () => {
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    fireEvent.click(screen.getByLabelText("Send answer"));
    expect(resolveNotice).not.toHaveBeenCalled();
  });

  it("dismiss submits action:'dismiss' with no reply", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    fireEvent.click(screen.getByRole("button", { name: "Close without answering" }));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 99, { action: "dismiss" }));
  });

  it("auto-advances to the next open notice after a send", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({
      awaiting: [n({ id: 1, title: "first", priority: "P0" }), n({ id: 2, title: "second", priority: "P1" })],
    });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    expect(screen.getByLabelText(/Reply to notice: first/)).not.toBeNull();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "a1" } });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 1, { action: "answer", reply: "a1" }));
    await waitFor(() => expect(screen.getByLabelText(/Reply to notice: second/)).not.toBeNull());
  });

  it("empty-box Down/Up arrows cycle the selection", async () => {
    setFeed({
      awaiting: [n({ id: 1, title: "first", priority: "P0" }), n({ id: 2, title: "second", priority: "P1" })],
    });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "ArrowDown" });
    await waitFor(() => expect(screen.getByLabelText(/Reply to notice: second/)).not.toBeNull());
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "ArrowUp" });
    await waitFor(() => expect(screen.getByLabelText(/Reply to notice: first/)).not.toBeNull());
  });
});

describe("InboxQueue — notice lifecycle", () => {
  it("hides legacy FYIs and never offers Mark read", () => {
    setFeed({ open: [ni({ id: 1, title: "migration done" })] });
    renderQueue([]);
    expect(screen.queryByText("migration done")).toBeNull();
    expect(screen.queryByText("Mark read")).toBeNull();
    expect(screen.getByText(/Nothing needs your attention/)).toBeTruthy();
  });

  it("surfaces a 409 as already answered", async () => {
    resolveNotice.mockRejectedValue(new Error("409 conflict"));
    setFeed({ awaiting: [n({ id: 5, agent_id: 1, title: "stale" })] });
    renderQueue([]);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "yes" } });
    fireEvent.click(screen.getByLabelText("Send answer"));
    await waitFor(() => expect(screen.getByText(/already answered/i)).toBeTruthy());
  });

  it("drops the dismissed notice from the open queue immediately", async () => {
    resolveNotice.mockResolvedValue({ status: "running" });
    setFeed({ awaiting: [n({ id: 1, title: "first" }), n({ id: 2, title: "second" })] });
    renderQueue([]);
    fireEvent.click(screen.getByRole("button", { name: "Close without answering" }));
    await waitFor(() => expect(dropOpenNotices).toHaveBeenCalledWith(expect.anything(), [1]));
  });
});
