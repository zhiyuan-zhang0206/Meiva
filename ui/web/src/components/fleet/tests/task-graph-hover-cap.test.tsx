// meiva:note
// The task graph's hover card: the grace window that lets the pointer reach the capped card, and
// the three data states (empty, cold failure, stale-while-error). The board reads its rows from the
// gateway through TanStack Query, so a test answers the fake transport and seeds the same cache the
// app fills; nothing replaces a hook module.

import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskListResponse, TaskRow } from "@/lib/contracts/types";

import { TaskGraph } from "../task-graph";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";

afterEach(cleanup);

// meiva:note
// The one replacement left in this file: the board navigates through Next's router, which needs a
// mounted app router. It leaves with the third-party-patch family.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const getTask = vi.fn<(id: number) => Promise<TaskRow>>();

beforeEach(() => {
  resetTransport();
  getTask.mockReset();
  getTask.mockResolvedValue(task(0));
  transport.getTask.mockImplementation((id: number) => getTask(id));
});

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
// The one door this file renders through: the rows the gateway would return are answered on the
// fake transport AND pre-seeded in the query cache, so the board paints them on the first frame and
// its mount refetch agrees instead of failing unanswered. `fetchRejects` is the unreachable gateway:
// with rows seeded that is the stale-while-error state, with none the cold failure.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function renderGraph(
  tasks: readonly TaskRow[],
  {
    props,
    settings,
    fetchRejects = false,
  }: {
    props?: Partial<ComponentProps<typeof TaskGraph>>;
    settings?: Record<string, unknown>;
    fetchRejects?: boolean;
  } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const window of TASK_WINDOWS) {
    queryClient.setQueryData<TaskListResponse>(["tasks", "graph", window], { tasks });
  }
  if (fetchRejects) {
    transport.getTasks.mockRejectedValue(new Error("gateway unavailable"));
  } else {
    transport.getTasks.mockResolvedValue({ tasks });
  }
  return renderWithProviders(
    <TaskGraph
      selectedAgentId={null}
      onSelectAgent={vi.fn()}
      selectedTaskId={null}
      onSelectTask={vi.fn()}
      {...props}
    />,
    { queryClient, settings, wallNow: () => Date.now() },
  );
}

function mockCardSize(width: number, height: number): () => void {
  const proto = HTMLElement.prototype;
  const widthDesc = Object.getOwnPropertyDescriptor({ offsetWidth: 0 }, "offsetWidth");
  const heightDesc = Object.getOwnPropertyDescriptor({ offsetHeight: 0 }, "offsetHeight");
  const isCard = function (this: HTMLElement) {
    return this.getAttribute("role") === "tooltip";
  };
  Object.defineProperty(proto, "offsetWidth", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return width;
      const real: unknown = widthDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  Object.defineProperty(proto, "offsetHeight", {
    configurable: true,
    get(this: HTMLElement) {
      if (isCard.call(this)) return height;
      const real: unknown = heightDesc?.get?.call(this);
      return typeof real === "number" ? real : 0;
    },
  });
  return () => {
    if (widthDesc) Object.defineProperty(proto, "offsetWidth", widthDesc);
    if (heightDesc) Object.defineProperty(proto, "offsetHeight", heightDesc);
  };
}

describe("TaskGraph hover card grace window and states", () => {
  it("keeps the capped card open when the pointer moves from the node onto it", async () => {
    const { container } = renderGraph(sampleTasks());
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 300);
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      fireEvent.mouseLeave(group, { relatedTarget: card.firstElementChild });
      expect(screen.getByRole("tooltip")).toBeTruthy();
      fireEvent.mouseLeave(card);
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      restore();
    }
  });

  it("gives the pointer a grace window to reach the capped card, then hides", async () => {
    const { container } = renderGraph(sampleTasks());
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 300);
    vi.useFakeTimers();
    try {
      fireEvent.mouseEnter(group);
      const card = screen.getByRole("tooltip");
      fireEvent.mouseLeave(group);
      expect(screen.getByRole("tooltip")).toBeTruthy();
      fireEvent.mouseEnter(card);
      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(screen.getByRole("tooltip")).toBeTruthy();
      fireEvent.mouseLeave(card);
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      vi.useRealTimers();
      restore();
    }
  });

  it("hides the capped card when the grace window expires without reaching it", async () => {
    const { container } = renderGraph(sampleTasks());
    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const group = texts[0].closest("g")!;
    const canvas = container.querySelector("svg[role='img']")!.parentElement!;
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 0, width: 320, height: 390 }),
    );
    vi.spyOn(group, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 130, y: 180, width: 36, height: 36 }),
    );
    const restore = mockCardSize(200, 300);
    vi.useFakeTimers();
    try {
      fireEvent.mouseEnter(group);
      fireEvent.mouseLeave(group);
      expect(screen.getByRole("tooltip")).toBeTruthy();
      act(() => {
        vi.advanceTimersByTime(250);
      });
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      vi.useRealTimers();
      restore();
    }
  });

  it("shows empty states for unset fields and a plain parent id for orphans", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      task(2, { title: "orphan", parent_id: 999 }),
    ];
    renderGraph(tasks);
    const texts = await waitFor(() => { const r = screen.getAllByText(/orphan/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });

    fireEvent.mouseEnter(texts[0].closest("g")!);
    const card = screen.getByRole("tooltip");
    const text = card.textContent;
    expect(text).toContain("Unowned");
    expect(text).toContain("#999");
    expect(text).toContain("Reminder");
    expect(text).not.toContain("every");
  });

  it("shows agent-created tasks by agent id", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      task(2, { title: "agent-made", parent_id: 1, created_by: "405", owner: 405 }),
    ];
    renderGraph(tasks);
    const texts = await waitFor(() => { const r = screen.getAllByText(/^agent-$/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });

    fireEvent.mouseEnter(texts[0].closest("g")!);
    const text = screen.getByRole("tooltip").textContent;
    expect(text).toContain("Agent #405");
  });

  it("empty task list (not loading, no error) shows the empty placeholder", () => {
    renderGraph([]);
    expect(screen.getByText("No tasks yet.")).toBeTruthy();
  });

  it("cold error (no tasks loaded) shows the error placeholder", async () => {
    renderGraph([], { fetchRejects: true });
    await waitFor(() => expect(screen.getByText("Failed to load tasks.")).toBeTruthy());
  });

  it("stale-while-error: error WITH tasks keeps the board (not the failure screen) and flags stale", async () => {
    renderGraph(sampleTasks(), { fetchRejects: true });

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.queryByText("Failed to load tasks.")).toBeNull();
    await waitFor(() => expect(screen.getByText("Stale")).toBeTruthy());
  });
});
