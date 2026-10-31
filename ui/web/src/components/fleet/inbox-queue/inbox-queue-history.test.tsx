import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { WallClockProvider } from "@/lib/transport/transport-context";
import type { ComponentProps } from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import type { AgentRow, NoticeItem, OpenNotice, PageRow, TaskSummaryRow } from "@/lib/contracts/types";
import {
  NOTICES_QUERY_KEY,
  NOTICES_RESOLVED_QUERY_KEY,
  type NoticesFeedWire,
} from "@/lib/notifications/use-notices";

// meiva:note
// What the queue is handed for one case: the rows the app would already hold in its caches, the
// registry under its seven-day window, the open pages, and which endpoint the case needs to behave
// badly. Every field is a value the real hooks turn into the answer the queue renders; no test
// replaces a hook module. `resolved` with `nextCursor` is the history's first page and `nextPage`
// the page that cursor reaches, `tasksWindow` the window those task rows answer under, `cold` an
// empty cache, and the two pairs of `…Fails` / `…Pending` flags are the endpoints refusing or
// never answering.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
interface QueueSeed {
  open?: NoticeItem[];
  awaiting?: NoticeItem[];
  resolved?: NoticeItem[];
  nextCursor?: { before_at: string; before_id: number } | null;
  nextPage?: NoticeItem[];
  tasks?: TaskSummaryRow[];
  tasksWindow?: string;
  pages?: PageRow[];
  cold?: boolean;
  openFails?: boolean;
  openPending?: boolean;
  historyFails?: boolean;
  historyPending?: boolean;
}

let seed: QueueSeed = {};

function setFeed(over: QueueSeed = {}) {
  seed = { ...seed, ...over };
}

import { InboxQueue } from "../inbox-queue";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getSettings.mockImplementation(() => Promise.resolve({ settings: [] }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  seed = {};
});

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

// meiva:note
// The endpoint that answers nothing: the case that needs a query still in flight (no rows, no
// error) hands this in, so "loading" is the hook's own state rather than a stubbed one.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function neverSettles<T>(): Promise<T> {
  return new Promise<T>(() => undefined);
}

// meiva:note
// The test door for this file: the cache carries the rows the app would have fetched, and the fake
// transport answers the same endpoints so a refetch agrees with it. The two hook modules stay real,
// so a case that reads a cursor or a failure is reading the hook's own answer.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function renderQueue(
  agents: AgentRow[],
  props: Omit<ComponentProps<typeof InboxQueue>, "agents"> = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const firstPage: NoticesFeedWire = {
    open: seed.open ?? [],
    awaiting: seed.awaiting ?? [],
    resolved_page: seed.cold ? [] : (seed.resolved ?? []),
    next_cursor: seed.nextCursor ?? null,
  };
  if (!seed.cold) {
    qc.setQueryData(NOTICES_QUERY_KEY, firstPage);
    qc.setQueryData(NOTICES_RESOLVED_QUERY_KEY, {
      pages: [{ resolved_page: firstPage.resolved_page, next_cursor: firstPage.next_cursor }],
      pageParams: [undefined],
    });
  }
  qc.setQueryData(["tasks", "summary", "7d"], { tasks: seed.tasksWindow === undefined || seed.tasksWindow === "7d" ? (seed.tasks ?? []) : [] });
  qc.setQueryData(["all-pages"], seed.pages ?? []);
  transport.getTasks.mockImplementation((opts?: { window?: string }) =>
    Promise.resolve({ tasks: (opts?.window ?? "all") === (seed.tasksWindow ?? "7d") ? (seed.tasks ?? []) : [] }),
  );
  transport.listAllPages.mockImplementation(() => Promise.resolve(seed.pages ?? []));
  transport.getNotices.mockImplementation((params?: { beforeAt?: string }) => {
    // meiva:note
    // The open feed asks with no cursor; the history asks with one, so the same endpoint answers
    // both the way the gateway does. A case that makes it fail or hang says which of the two.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    const history = params !== undefined;
    if (history ? seed.historyPending : seed.openPending) return neverSettles();
    if (history ? seed.historyFails : seed.openFails) return Promise.reject(new Error("inbox unavailable"));
    if (params?.beforeAt !== undefined) {
      return Promise.resolve({ open: [], awaiting: [], resolved_page: seed.nextPage ?? [], next_cursor: null });
    }
    return Promise.resolve(firstPage);
  });
  return renderWithBrowser(
    // meiva:note
    // The queue's rows age against the page's clock; the app's own answer (the system clock) is
    // what this file hands in, so its `setSystemTime` cases keep deciding the ages.
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>
        <InboxQueue agents={agents} {...props} />
      </QueryClientProvider>
    </WallClockProvider>,
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
    expect(screen.queryByText("Decision")).toBeNull();
  });

  it("labels an expired resolution", () => {
    setFeed({ resolved: [ni({ id: 52, title: "timed out", require_response: true, resolution: "expired", reply: null })] });
    renderQueue([]);
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByText("timed out"));
    expect(screen.getByText(/Expired/)).not.toBeNull();
  });

  // meiva:note
  // "Show more" is the history's own cursor: the first page says another follows, the endpoint
  // answers that cursor with the second page, and the second page's row appears.
  // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
  it("pages back with Show more once the disclosure is open", async () => {
    setFeed({
      resolved: [ni({ id: 60, title: "r1" })],
      nextCursor: { before_at: "2026-06-18T00:00:00Z", before_id: 60 },
      nextPage: [ni({ id: 59, title: "r2" })],
    });
    renderQueue([]);
    expect(screen.queryByRole("button", { name: /Show more/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Resolved/ }));
    fireEvent.click(screen.getByRole("button", { name: /Show more/ }));
    await screen.findByText("r2");
    expect(screen.getByText("r1")).not.toBeNull();
    expect(transport.getNotices).toHaveBeenCalledWith(
      expect.objectContaining({ beforeAt: "2026-06-18T00:00:00Z", beforeId: 60 }),
    );
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
  function withRegistry(window = "7d") {
    setFeed({
      tasksWindow: window,
      tasks: [
        tk(1, { title: "root" }),
        tk(2, { parent_id: 1, title: "ship feature", owner: 30 }),
        tk(3, { parent_id: 2, title: "write tests", owner: 40 }),
      ],
    });
  }

  // meiva:note
  // The binding is the window the queue asks under, so the case states it as one: a registry that
  // only answers under a different window reaches the queue empty, and the same rows under seven
  // days group. A hook replaced by a test could not tell those two apart.
  // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
  it("reads the task registry under the seven-day window and under no other", () => {
    withRegistry("30d");
    setFeed({ awaiting: [n({ id: 1, title: "q-mgr", priority: "P2", agent_id: 30 })] });
    const other = renderQueue([agent({ agent_id: 30, label: "mgr" })]);
    expect(screen.queryByLabelText(/^Task #/)).toBeNull();
    other.unmount();
    setFeed({ tasksWindow: undefined });
    withRegistry();
    setFeed({ awaiting: [n({ id: 1, title: "q-mgr", priority: "P2", agent_id: 30 })] });
    renderQueue([agent({ agent_id: 30, label: "mgr" })]);
    expect(screen.getByLabelText("Task #2: ship feature")).not.toBeNull();
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
  it("open feed failure with nothing loaded → failure screen, not the empty state", async () => {
    setFeed({ cold: true, openFails: true });
    renderQueue([agent({ agent_id: 1 })]);
    await screen.findByText("Failed to load inbox.");
    expect(screen.getByText("Failed to load inbox.")).not.toBeNull();
    expect(screen.queryByText(/Nothing needs your attention/)).toBeNull();
  });

  it("open feed failure with notices on screen → Stale badge, entries still shown", async () => {
    setFeed({ openFails: true, awaiting: [n({ id: 10, title: "dec-p2" })] });
    renderQueue([agent({ agent_id: 1, label: "a" })]);
    await screen.findByText("Stale");
    expect(screen.getByText("Stale")).not.toBeNull();
    expect(listTitles()).toEqual(["dec-p2"]);
  });

  it("resolved-history failure with open entries on screen → unavailable line", async () => {
    setFeed({ historyFails: true, awaiting: [n({ id: 21, title: "question" })] });
    renderQueue([agent({ agent_id: 1 })]);
    await screen.findByText("Resolved history unavailable.");
    expect(screen.getByText("Resolved history unavailable.")).not.toBeNull();
    expect(listTitles()).toEqual(["question"]);
  });

  it("any cold failure with nothing at all loaded → failure screen", async () => {
    setFeed({ cold: true, historyFails: true });
    renderQueue([agent({ agent_id: 1 })]);
    await screen.findByText("Failed to load inbox.");
    expect(screen.getByText("Failed to load inbox.")).not.toBeNull();
  });

  it("first load in flight → Loading, not the empty state", () => {
    setFeed({ cold: true, openPending: true, historyPending: true });
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
    setFeed({ pages: [mkPage({ agent_id: 7, name: "dash", url: "http://host/dash" })], awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const list = screen.getByRole("list", { name: /inbox queue/i });
    const link = within(list).getByRole("link", { name: /open live page/i });
    expect(link.getAttribute("href")).toBe("http://host/dash");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("no page link on a row whose agent has no open page", () => {
    setFeed({ pages: [mkPage({ agent_id: 2, name: "other" })] });
    setFeed({ awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const list = screen.getByRole("list", { name: /inbox queue/i });
    expect(within(list).queryByRole("link", { name: /open live page/i })).toBeNull();
  });

  it("the detail header also links the selected agent's live page", () => {
    setFeed({ pages: [mkPage({ agent_id: 7, name: "dash", url: "http://host/dash" })], awaiting: [n({ id: 99, title: "Q?" })] });
    renderQueue([agent({ agent_id: 7, label: "lead" })]);
    const links = screen.getAllByRole("link", { name: /open live page/i });
    expect(links.length).toBe(2);
    expect(links.every((l) => l.getAttribute("href") === "http://host/dash")).toBe(true);
  });
});
