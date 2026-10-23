import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentRow, NoticeItem, OpenNotice, PageRow, TaskSummaryRow } from "@/lib/contracts/types";
import type { NoticesFeed } from "@/lib/notifications/use-notices";

const resolveNotice = vi.fn<
  (agentId: number, noticeId: number, body: { action: string; reply?: string }) => Promise<{ status: string }>
>();
vi.mock("@/lib/transport/api", () => ({
  api: {
    resolveNotice: (a: number, n: number, b: { action: string; reply?: string }) => resolveNotice(a, n, b),
    getSettings: () => Promise.resolve({ settings: [] }),
  },
}));

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

const scrollIntoViewMock = vi.fn();
Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
  configurable: true,
  value: scrollIntoViewMock,
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

function mkPage(over: Partial<PageRow> & { agent_id: number; name: string }): PageRow {
  return {
    id: 1,
    port: 10000,
    title: over.name,
    serve_dir: null,
    url: `http://host/${over.name}`,
    created_at: "2026-01-01T00:00:00Z",
    closed_at: null,
    ...over,
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
  return render(
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

describe("InboxQueue — resolved history (collapsed disclosure)", () => {
  it("keeps Markdown formatting in resolved titles and original choices", async () => {
    setFeed({ resolved: [{
      ...ni({ id: 50, title: "Choose **platform**", require_response: true, resolution: "answered" }),
      question: { type: "single_select", options: [{ id: "web", label: "**Web**", description: "Use `browser`." }], allow_other: false },
      answer: { option_ids: ["web"], comment: "Keep **this** literal." },
    }] });
    renderQueue([agent({ agent_id: 1 })]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    const row = screen.getByTestId("notice-title");
    await waitFor(() => expect(row.querySelector("strong")?.textContent).toBe("platform"));
    fireEvent.click(row);
    expect(screen.getByRole("heading", { name: "Choose platform" }).querySelector("strong")?.textContent).toBe("platform");
    const options = screen.getByRole("list", { name: "Original numbered options" });
    expect(options.querySelector("strong")?.textContent).toBe("Web");
    expect(options.querySelector("code")?.textContent).toBe("browser");
    expect(screen.getByText("Keep **this** literal.")).toBeTruthy();
  });

  it("keeps the resolved history behind a collapsed disclosure, off the main stream", () => {
    setFeed({ resolved: [ni({ id: 50, title: "old notice", resolution: "answered", reply: "my recorded answer" })] });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.queryByText("old notice")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByText("old notice"));
    expect(screen.getByText(/my recorded answer/)).not.toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("shows the word 'Resolved' without a count badge (user ruling 2026-08-09 #1096)", () => {
    setFeed({ resolved: [ni({ id: 50, title: "old notice" }), ni({ id: 51, title: "older notice" })] });
    renderQueue([agent({ agent_id: 1 })]);
    const btn = screen.getByRole("button", { name: "Resolved" });
    expect(btn.getAttribute("aria-label") ?? btn.textContent).toBe("Resolved");
    expect(btn.textContent).not.toMatch(/\d/);
  });

  it("stamps a resolved row with its agent label and creation time, not its resolution time", () => {
    setFeed({
      resolved: [
        ni({
          id: 50,
          title: "old notice",
          agent_label: "worker",
          created_at: "2026-06-17T00:00:00Z",
          resolved_at: "2026-06-18T09:30:00Z",
          resolution: "answered",
        }),
      ],
    });
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-18T10:00:00Z"));
    renderQueue([agent({ agent_id: 1 })]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    const list = screen.getByRole("list", { name: /inbox queue/i });
    const row = within(list).getByText("old notice").closest("button");
    expect(row?.textContent).toContain("worker");
    expect(row?.textContent).toContain("1d ago");
    expect(row?.textContent).not.toContain("30m ago");
    vi.useRealTimers();
  });

  it("keeps the resolution inline with the agent's text, not pinned to the panel bottom", () => {
    setFeed({ resolved: [ni({ id: 50, title: "old notice", content: "agent said this", resolution: "answered", reply: "my recorded answer" })] });
    renderQueue([]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByText("old notice"));

    const scroller = screen.getByText(/my recorded answer/).closest(".overflow-y-auto");
    expect(scroller).not.toBeNull();
    expect(scroller!.contains(screen.getByText("agent said this"))).toBe(true);
  });

  it("labels a dismissed resolution", () => {
    setFeed({ resolved: [ni({ id: 51, title: "skipped", require_response: true, resolution: "dismissed", reply: null })] });
    renderQueue([]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByText("skipped"));
    expect(screen.getByText(/Dismissed/)).not.toBeNull();
  });

  it("labels an expired resolution", () => {
    setFeed({ resolved: [ni({ id: 52, title: "timed out", require_response: true, resolution: "expired", reply: null })] });
    renderQueue([]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByText("timed out"));
    expect(screen.getByText(/Expired/)).not.toBeNull();
  });

  it("pages back with Show more once the disclosure is open", () => {
    const fetchNextPage = vi.fn();
    setFeed({ resolved: [ni({ id: 60, title: "r1" })], hasNextPage: true, fetchNextPage });
    renderQueue([]);
    expect(screen.queryByRole("button", { name: /Show more/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByRole("button", { name: /Show more/ }));
    expect(fetchNextPage).toHaveBeenCalled();
  });
});

function tk(id: number, over: Partial<TaskSummaryRow> = {}): TaskSummaryRow {
  return {
    id,
    parent_id: null,
    title: `Task ${id}`,
    status: "in_progress",
    priority: "P2",
    owner: null,
    created_by: "user",
    created_at: "2026-06-14T00:00:00Z",
    updated_at: "2026-06-14T00:00:00Z",
    reminder_count: 0,
    ...over,
  };
}

describe("InboxQueue — task grouping", () => {
  function withRegistry() {
    useTasksMock.mockReturnValue({
      tasks: [
        tk(1, { title: "root" }),
        tk(2, { parent_id: 1, title: "ship feature", owner: 30 }),
        tk(3, { parent_id: 2, title: "write tests", owner: 40 }),
      ],
      loading: false,
      error: false,
    });
  }

  it("bounds task grouping to the seven-day registry window", () => {
    withRegistry();
    renderQueue([]);
    expect(useTasksMock).toHaveBeenCalledWith("7d", "summary");
  });

  it("groups entries under the top-level task subtree with a count", () => {
    withRegistry();
    setFeed({
      awaiting: [
        n({ id: 1, title: "q-mgr", priority: "P2", agent_id: 30 }),
        n({ id: 2, title: "q-wrk", priority: "P3", agent_id: 40 }),
      ],
    });
    renderQueue([agent({ agent_id: 30, label: "mgr" }), agent({ agent_id: 40, label: "wrk" })]);
    const header = screen.getByLabelText("Task #2: ship feature");
    expect(header.textContent).toContain("#2 ship feature");
    expect(header.textContent).toContain("2");
    expect(listTitles()).toEqual(["q-mgr", "q-wrk"]);
  });

  it("pulls a subtree together at its best-ranked entry; loose agents stay flat", () => {
    withRegistry();
    setFeed({
      awaiting: [
        n({ id: 5, title: "q-loose", priority: "P1", agent_id: 999 }),
        n({ id: 6, title: "q-mgr", priority: "P2", agent_id: 30 }),
        n({ id: 7, title: "q-wrk", priority: "P0", agent_id: 40 }),
      ],
    });
    renderQueue([
      agent({ agent_id: 999, label: "loose" }),
      agent({ agent_id: 30, label: "mgr" }),
      agent({ agent_id: 40, label: "wrk" }),
    ]);
    expect(listTitles()).toEqual(["q-wrk", "q-mgr", "q-loose"]);
    expect(screen.getByLabelText("Task #2: ship feature")).not.toBeNull();
  });

  it("renders no group headers when the registry is empty", () => {
    setFeed({ awaiting: [n({ id: 1, title: "q1", agent_id: 30 })] });
    renderQueue([agent({ agent_id: 30, label: "mgr" })]);
    expect(screen.queryByLabelText(/^Task #/)).toBeNull();
  });
});

describe("InboxQueue — error presentation (audit C3)", () => {
  it("open feed failure with nothing loaded → failure screen, not the empty state", () => {
    setFeed({ error: true });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText("Failed to load inbox.")).not.toBeNull();
    expect(screen.queryByText(/Nothing needs your attention/)).toBeNull();
  });

  it("open feed failure with notices on screen → Stale badge, entries still shown", () => {
    setFeed({ error: true, awaiting: [n({ id: 10, title: "dec-p2" })] });
    renderQueue([agent({ agent_id: 1, label: "a" })]);
    expect(screen.getByText("Stale")).not.toBeNull();
    expect(listTitles()).toEqual(["dec-p2"]);
  });

  it("resolved-history failure with open entries on screen → unavailable line", () => {
    setFeed({ resolvedError: true, awaiting: [n({ id: 21, title: "question" })] });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText("Resolved history unavailable.")).not.toBeNull();
    expect(listTitles()).toEqual(["question"]);
  });

  it("any cold failure with nothing at all loaded → failure screen", () => {
    setFeed({ resolvedError: true });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText("Failed to load inbox.")).not.toBeNull();
  });

  it("first load in flight → Loading, not the empty state", () => {
    setFeed({ isLoading: true });
    renderQueue([agent({ agent_id: 1 })]);
    expect(screen.getByText("Loading…")).not.toBeNull();
    expect(screen.queryByText(/Nothing needs your attention/)).toBeNull();
  });
});

describe("InboxQueue — terminated agent", () => {
  it("offers no resurrect button for a terminated agent's notice", () => {
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "dead", status: "terminated" })]);
    expect(screen.getAllByText("Q?").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /Resurrect Agent #7/ })).toBeNull();
  });
});

describe("InboxQueue — agent live-page entry", () => {
  it("a queue row links to the agent's live page (fleet-wide, no per-row fetch)", () => {
    useAllPagesMock.mockReturnValue([mkPage({ agent_id: 7, name: "dash", url: "http://host/dash" })]);
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const list = screen.getByRole("list", { name: /inbox queue/i });
    const link = within(list).getByRole("link", { name: /open live page/i });
    expect(link.getAttribute("href")).toBe("http://host/dash");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("no page link on a row whose agent has no open page", () => {
    useAllPagesMock.mockReturnValue([mkPage({ agent_id: 2, name: "other" })]);
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const list = screen.getByRole("list", { name: /inbox queue/i });
    expect(within(list).queryByRole("link", { name: /open live page/i })).toBeNull();
  });

  it("the detail header also links the selected agent's live page", () => {
    useAllPagesMock.mockReturnValue([mkPage({ agent_id: 7, name: "dash", url: "http://host/dash" })]);
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const links = screen.getAllByRole("link", { name: /open live page/i });
    expect(links.length).toBe(2);
    expect(links.every((l) => l.getAttribute("href") === "http://host/dash")).toBe(true);
  });
});
