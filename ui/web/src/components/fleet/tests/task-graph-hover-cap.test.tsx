import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskRow } from "@/lib/contracts/types";
import type { TasksResult } from "@/lib/notifications/use-tasks";
import { resetMockSettings } from "@/test-support/user-settings-mock";

import { TaskGraph } from "../task-graph";
import { renderWithApi as render } from "@/test-support/render";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getTask.mockImplementation((id: number) => getTask(id));
});


const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
// meiva:note
// The hover card ages its rows on the page's clock; the real one keeps these geometry assertions
// as they were.
const wrapGraph = (ui: ReactElement) => (
  <WallClockProvider wallNow={() => Date.now()}>
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
  </WallClockProvider>
);

vi.mock("@/lib/state/use-user-settings", () => import("@/test-support/user-settings-mock"));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const getTask = vi.fn<(id: number) => Promise<TaskRow>>();

const useTasks = vi.fn<(...args: string[]) => TasksResult>();
vi.mock("@/lib/notifications/use-tasks", () => ({
  useTasks: (...args: string[]) => useTasks(...args),
}));

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

function ok(tasks: TaskRow[]): TasksResult {
  return { tasks, loading: false, error: false };
}

beforeEach(() => {
  queryClient.clear();
  useTasks.mockReset();
  getTask.mockReset();
  getTask.mockResolvedValue(task(0));
  resetMockSettings();
});

afterEach(cleanup);

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
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
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
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
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
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
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
    useTasks.mockReturnValue(ok(tasks));
    render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
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
    useTasks.mockReturnValue(ok(tasks));
    render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    const texts = await waitFor(() => { const r = screen.getAllByText(/^agent-$/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });

    fireEvent.mouseEnter(texts[0].closest("g")!);
    const text = screen.getByRole("tooltip").textContent;
    expect(text).toContain("Agent #405");
  });

  it("empty task list (not loading, no error) shows the empty placeholder", () => {
    useTasks.mockReturnValue({ tasks: [], loading: false, error: false });
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    expect(screen.getByText("No tasks yet.")).toBeTruthy();
  });

  it("cold error (no tasks loaded) shows the error placeholder", () => {
    useTasks.mockReturnValue({ tasks: [], loading: false, error: true });
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    expect(screen.getByText("Failed to load tasks.")).toBeTruthy();
  });

  it("stale-while-error: error WITH tasks keeps the board (not the failure screen) and flags stale", async () => {
    useTasks.mockReturnValue({ tasks: sampleTasks(), loading: false, error: true });
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.queryByText("Failed to load tasks.")).toBeNull();
    expect(screen.getByText("Stale")).toBeTruthy();
  });
});
