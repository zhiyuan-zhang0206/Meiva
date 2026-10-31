// meiva:note
// TaskGraph tests: the free force-directed graph plus the Kanban column view. Like the Graph View
// tests, the d3-force layout is a real, time-driven simulation; these tests drive it just far enough
// to settle card positions, then assert the SVG / DOM the component renders. Rows arrive the way the
// app supplies them (the query cache, answered by the fake transport), so no hook module is replaced.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md

import { QueryClient } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskListResponse, TaskRow, UserSettingRow } from "@/lib/contracts/types";

import { TaskGraph } from "./task-graph";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";

// meiva:note
// The route hands its surfaces a `navigate`; a suite passes a spy, so a jump can be read
// without replacing `next/navigation`.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
const navigate = vi.fn();

const getTask = vi.fn<(id: number) => Promise<TaskRow>>();

function task(id: number, over: Partial<TaskRow> = {}): TaskRow {
  return {
    id,
    parent_id: null,
    title: `Task ${id}`,
    description: `Description for task ${id}`,
    results: null,
    status: "in_progress",
    priority: "P2",
    owner: null,
    created_by: "user",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    reminder_count: 0,
    ...over,
  };
}

function sampleTasks(): TaskRow[] {
  return [
    task(1, { title: "root", status: "in_progress" }),
    task(2, { title: "subtask-active", status: "in_progress", parent_id: 1 }),
    task(3, { title: "subtask-ip", status: "in_progress", parent_id: 1 }),
    task(4, { title: "subtask-done", status: "done", parent_id: 1 }),
    task(5, { title: "subtask-cancelled", status: "cancelled", parent_id: 1 }),
  ];
}

const TASK_WINDOWS = ["24h", "7d", "30d", "all"] as const;

// meiva:note
// The one door this file renders through: the rows the gateway would return are answered on the fake
// transport AND pre-seeded in the query cache, so the board paints them on the first frame and its
// mount refetch agrees. Preferences come from the settings cache the app fills, and the settings
// surface answers reads and writes, so a clicked mode or toggle survives its reconcile fetch.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function renderGraph(
  tasks: readonly TaskRow[],
  {
    props,
    settings,
  }: {
    props?: Partial<ComponentProps<typeof TaskGraph>>;
    settings?: Record<string, unknown>;
  } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const window of TASK_WINDOWS) {
    queryClient.setQueryData<TaskListResponse>(["tasks", "graph", window], { tasks });
  }
  transport.getTasks.mockResolvedValue({ tasks });
  const stored = new Map<string, unknown>(Object.entries(settings ?? {}));
  const row = (key: string, value: unknown): UserSettingRow => ({
    key,
    value,
    updated_at: "2026-01-01T00:00:00Z",
  });
  transport.getSettings.mockImplementation(() =>
    Promise.resolve({ settings: [...stored].map(([key, value]) => row(key, value)) }),
  );
  transport.putSetting.mockImplementation((key: string, value: unknown) => {
    stored.set(key, value);
    return Promise.resolve(row(key, value));
  });
  return renderWithProviders(
    <TaskGraph
      selectedAgentId={null}
      onSelectAgent={vi.fn()}
      selectedTaskId={null}
      onSelectTask={vi.fn()}
      {...props}
    navigate={navigate}
    />,
    { queryClient, settings, wallNow: () => Date.now() },
  );
}

beforeEach(() => {
  resetTransport();
  getTask.mockReset();
  getTask.mockResolvedValue(task(0));
  transport.getTask.mockImplementation((id: number) => getTask(id));
});

afterEach(cleanup);

describe("TaskGraph hover detail card", () => {
  // The instant hover card replaces the old native SVG <title> (whose
  // appearance the browser deferred — the perceived hover lag): it must show
  // the task's registry fields the moment the cursor enters the node.
  it("shows the detail card instantly on hover, with the registry fields", async () => {
    const fullTask = task(2, {
        title: "Build the widget",
        description: "A longer **description**\nspanning two lines",
        results: "Shipped in #1",
        status: "in_progress",
        priority: "P1",
        parent_id: 1,
        owner: 30,
        owner_label: "builder",
        created_by: "user",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z",
        remind_interval_seconds: 7200,
        last_reminded_at: "2026-01-02T00:00:00Z",
        reminder_count: 3,
      });
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      fullTask,
    ];
    getTask.mockResolvedValue(fullTask);
    const { container } = renderGraph(tasks);

    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;

    // No native <title> remains — the delayed browser tooltip is gone.
    expect(container.querySelectorAll("svg title").length).toBe(0);

    // The card appears synchronously with the mouseenter (no delay timer).
    fireEvent.mouseEnter(group);
    expect(screen.queryByRole("tooltip")).not.toBeNull();
    const card = screen.getByRole("tooltip");
    const text = card.textContent;
    expect(text).toContain("Build the widget");
    expect(text).toContain("Task #2");
    expect(text).toContain("In progress");
    expect(text).toContain("P1");
    expect(text).toContain("builder");
    expect(text).toContain("#30");
    expect(text).toContain("User"); // created_by
    expect(text).toContain("root (#1)"); // parent
    expect(text).toContain("every 2h"); // remind interval
    expect(text).toContain("3 reminders");
    // meiva:note
    // The list projection carries no text columns; the card fetches them.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    await waitFor(() => expect(screen.getByRole("tooltip").textContent).toContain("A longer description"));
    const fetched = screen.getByRole("tooltip");
    expect(fetched.textContent).toContain("Shipped in #1");
    // Task description / results are Markdown-authored: rendered, not raw.
    expect(fetched.textContent).not.toContain("**");
    expect(card.querySelector("strong")?.textContent).toBe("description");
    expect(getTask).toHaveBeenCalledWith(2);
    expect(text).toContain("Double-click the node to open the owner");
  });

  it("says so in one line when the full row cannot be read", async () => {
    const tasks: TaskRow[] = [task(1, { title: "root" }), task(2, { title: "no-detail", parent_id: 1 })];
    getTask.mockRejectedValue(new Error("offline"));
    renderGraph(tasks);
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    fireEvent.mouseEnter(texts[0].closest("g")!);
    await waitFor(() => expect(screen.getByTestId("task-detail-failed")).toBeTruthy());
    expect(screen.getByTestId("task-detail-failed").textContent).toContain("Could not load the full task");
    // meiva:note
    // The registry fields still render; only the text sections are missing.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    expect(screen.getByRole("tooltip").textContent).toContain("no-detail");
  });

  // meiva:note
  // A card the pointer cannot reach (not height-capped) hides the moment the pointer leaves the
  // node, with no grace window; that decision is hoverCardLeave() in force-graph-geometry and is
  // covered there. Its twin — the grace window a scrollable card waits out — is exercised in
  // tests/task-graph-hover-cap.test.tsx.
  // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md

  it("keeps the card anchored to the node while the cursor moves (no following)", async () => {
    // User ruling 2026-08-29: the detail card shows on mouseenter and hides
    // on mouseleave — it must not chase the cursor. The card is pinned to
    // the node's on-screen box; a mousemove over the node leaves it put.
    renderGraph(sampleTasks());
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;

    fireEvent.mouseEnter(group);
    const card = screen.getByRole("tooltip");
    const leftAfterEnter = card.style.left;

    // A far-away mousemove must not move the card (jsdom boxes are all 0×0,
    // so the card sits at the clamped 4px anchor; cursor-following would
    // rewrite left to follow clientX).
    fireEvent.mouseMove(group, { clientX: 900, clientY: 900 });
    expect(screen.getByRole("tooltip")).toBeTruthy();
    expect(card.style.left).toBe(leftAfterEnter);
  });

});
