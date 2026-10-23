
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskRow } from "@/lib/contracts/types";
import type { TasksResult } from "@/lib/notifications/use-tasks";
import { mockSetSettingCalls, resetMockSettings } from "@/test-support/user-settings-mock";

import { FORCE_DEFAULTS, FORCE_GROUPS, TASK_FORCE_GROUPS, type ForceGroup } from "../force-controls";
import { STATUS_TO_LANE } from "../task-kanban";
import { STATUS_FILL, TASK_LEGEND_ENTRIES } from "../presentation/task-palette";
import { TASK_FORCE_KEY } from "../task-graph-chrome";
import { TaskGraph } from "../task-graph";

// meiva:note
// The hover card reads its one row through TanStack Query, so the hover tests
// wrap their render in a client; the other tests never mount the card.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
// meiva:note
// Lower-case helper (not a component): the hover tests need the Query client the
// card's one-row read runs in; the rest never mount the card.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
const wrapGraph = (ui: ReactElement) => (
  <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
);

vi.mock("@/lib/state/use-user-settings", () => import("@/test-support/user-settings-mock"));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

const getTask = vi.fn<(id: number) => Promise<TaskRow>>();
vi.mock("@/lib/transport/api", async (importOriginal) => {
  const original = await importOriginal<{ api: Record<string, unknown> }>();
  return { ...original, api: { ...original.api, getTask: (id: number) => getTask(id) } };
});

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
  // meiva:note
  // Reset persisted graph/kanban mode + toggle state (now DB-backed user
  // settings) so a previous test's mode switch or toggle doesn't leak.
  // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
  resetMockSettings();
});

afterEach(cleanup);


it("maps each task status into its Kanban lane", () => {
  expect(STATUS_TO_LANE.in_progress).toBe(0);
  expect(STATUS_TO_LANE.done).toBe(1);
  expect(STATUS_TO_LANE.cancelled).toBe(2);
});

describe("TaskGraph (graph mode)", () => {
  it("uses full rows so graph and kanban retain task text", () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    expect(useTasks.mock.calls[0]?.slice(0, 2)).toEqual(["24h", "graph"]);
  });

  it("legend lists only the categories the canvas renders", () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    const legend = screen.getByLabelText("Task graph legend");
    expect(legend.textContent).toContain("In progress");
    expect(legend.textContent).toContain("Root");
    expect(legend.textContent).not.toContain("Done");
    expect(legend.textContent).not.toContain("Canceled");
    expect(legend.textContent).not.toContain("Ongoing");
    expect(legend.querySelector("span.text-slate-400")).toBeTruthy();
    expect(legend.querySelector("span.text-violet-500")).toBeTruthy();
    expect(legend.textContent).not.toContain("Uniform node size");
  });

  it("status set and legend stay single-sourced (displayable == backend set)", () => {
    expect(Object.keys(STATUS_FILL).sort()).toEqual(["cancelled", "done", "in_progress"]);
    expect(TASK_LEGEND_ENTRIES.map((entry) => entry.key)).toEqual(["in_progress", "root"]);
    expect(TASK_LEGEND_ENTRIES.find((entry) => entry.key === "root")?.fill).toBe("text-violet-500");
    expect(TASK_LEGEND_ENTRIES.find((entry) => entry.key === "in_progress")?.fill).toBe(
      STATUS_FILL.in_progress,
    );
  });

  it("renders the root violet, active tasks slate — and no toggle-hidden category by default", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(document.querySelectorAll("rect.text-violet-500").length).toBe(1);
    expect(document.querySelectorAll("rect.text-slate-400").length).toBeGreaterThan(0);
    expect(document.querySelector("rect.text-emerald-500")).toBeNull();
    expect(document.querySelector("rect.text-destructive")).toBeNull();
  });

  it("renders only in-progress cards by default", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.getAllByText(/#3/).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getAllByText(/#1/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.queryByText(/#4/)).toBeNull();
    expect(screen.queryByText(/#5/)).toBeNull();
  });

  it("has no swimlane band labels in graph mode", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });

    for (const lane of ["Backlog", "In progress", "Done"]) {
      const svgTexts = document.querySelectorAll("svg text");
      const found = Array.from(svgTexts).some((el) => el.textContent.trim() === lane);
      expect(found).toBe(false);
    }
  });

  it("shows Graph/Kanban mode toggle", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.getByText("Kanban")).toBeTruthy();
    expect(screen.getByText("Graph")).toBeTruthy();
  });

  it("switches to Kanban mode and shows only visible columns", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });

    fireEvent.click(screen.getByText("Kanban"));

    await waitFor(() => expect(screen.getAllByText("In progress").length).toBeGreaterThanOrEqual(1));
    expect(screen.getByText("Description for task 2")).toBeTruthy();
  });

  it("in Kanban mode, Done column appears when Done toggle is on", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      task(2, { title: "child", status: "in_progress", parent_id: 1 }),
      task(3, { title: "done-child", status: "done", parent_id: 1 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });

    fireEvent.click(screen.getByText("Kanban"));
    expect(screen.getByText("Done")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /^Done/ }));
    await waitFor(() => expect(screen.getAllByText(/#3/).length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("shows Done tasks when the Done toggle is clicked (graph mode)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.queryByText(/#4/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Done/ }));
    await waitFor(() => expect(screen.getAllByText(/#4/).length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("shows Canceled tasks when the Canceled toggle is clicked (graph mode)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);

    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.queryByText(/#5/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Canceled/ }));
    await waitFor(() => expect(screen.getAllByText(/#5/).length).toBeGreaterThan(0), { timeout: 4000 });
  });

  it("selects a card on single click", async () => {
    const onSelect = vi.fn();
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={onSelect} />);

    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const text = texts[0];
    const card = text.closest("g")!;

    fireEvent.click(card);
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it("clicking an already-selected card is a no-op (does not deselect)", async () => {
    const onSelect = vi.fn();
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={2} onSelectTask={onSelect} />);

    const texts = await waitFor(() => { const r = screen.getAllByText(/#2/); expect(r.length).toBeGreaterThan(0); return r; }, { timeout: 4000 });
    const text = texts[0];
    fireEvent.click(text.closest("g")!);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("propagates the owner of an externally selected task (route jump, task #2909)", async () => {
    const onSelectAgent = vi.fn();
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(2, { title: "jumped-into", parent_id: 1, owner: 7 }),
      ]),
    );
    render(
      <TaskGraph
        selectedAgentId={null}
        onSelectAgent={onSelectAgent}
        selectedTaskId={2}
        onSelectTask={vi.fn()}
      />,
    );

    await waitFor(() => expect(onSelectAgent).toHaveBeenCalledWith(7));
  });

  it("does not re-propagate the owner when the agent is already selected", async () => {
    const onSelectAgent = vi.fn();
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(2, { title: "jumped-into", parent_id: 1, owner: 7 }),
      ]),
    );
    render(
      <TaskGraph
        selectedAgentId={7}
        onSelectAgent={onSelectAgent}
        selectedTaskId={2}
        onSelectTask={vi.fn()}
      />,
    );

    await waitFor(() => expect(useTasks).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(onSelectAgent).not.toHaveBeenCalled();
  });

});

describe("TaskGraph layout controls", () => {
  const surface = (groups: ForceGroup[]) =>
    groups.map((g) => ({ label: g.label, sliders: g.sliders.map((s) => `${s.key}:${s.label}`) }));

  it("Node group is a single Size slider — no min/max band (ruling 2026-08-10 #1127)", () => {
    const nodeGroup = TASK_FORCE_GROUPS.find((g) => g.label === "node")!;
    expect(nodeGroup.sliders).toEqual([{ key: "nodeSizeMin", label: "size", min: 6, max: 48, step: 1 }]);
    expect(surface(TASK_FORCE_GROUPS.slice(1))).toEqual(surface(FORCE_GROUPS.slice(1)));
    expect(TASK_FORCE_GROUPS.map((g) => g.label)).toEqual(["node", "edge", "layout", "zoom"]);
  });

  it("repulsion max is at least 10000 on BOTH graphs (user ruling 2026-08-10 #1140)", () => {
    const repulsion = (groups: ForceGroup[]) =>
      groups
        .find((g) => g.label === "layout")!
        .sliders.find((s) => s.key === "repulsion")!;
    expect(repulsion(FORCE_GROUPS).max).toBeGreaterThanOrEqual(10000);
    expect(repulsion(TASK_FORCE_GROUPS).max).toBeGreaterThanOrEqual(10000);
  });

  it("persists to its OWN key — display.task_force_params.v2, not the agent graph's", () => {
    expect(TASK_FORCE_KEY).toBe("display.task_force_params.v2");
    expect(TASK_FORCE_KEY).not.toBe("display.graph_force_params");
  });

  it("every default sits inside its slider's range", () => {
    for (const group of [...FORCE_GROUPS, ...TASK_FORCE_GROUPS]) {
      for (const s of group.sliders) {
        const v = FORCE_DEFAULTS[s.key];
        expect(`${s.key}=${v}`).toBe(`${s.key}=${Math.min(Math.max(v, s.min), s.max)}`);
      }
    }
  });

  it("square size follows the single Size setting (own key, ruling 2026-08-10 #1127)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const rect = container.querySelector("svg[role='img'] rect")!;
    expect(rect.getAttribute("width")).toBe("36");

    cleanup();
    resetMockSettings({
      "display.task_force_params.v2": { ...FORCE_DEFAULTS, nodeSizeMin: 36 },
    });
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container: c2 } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const rect2 = c2.querySelector("svg[role='img'] rect")!;
    expect(rect2.getAttribute("width")).toBe("72");
  });

  it("task nodes are uniform size regardless of subtree size (user ruling 2026-08-09 #1070)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />),
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const rects = [...container.querySelectorAll("svg[role='img'] rect")];
    expect(rects.length).toBeGreaterThanOrEqual(3);
    const widths = new Set(rects.map((r) => r.getAttribute("width")));
    expect(widths.size).toBe(1);
    expect([...widths][0]).toBe("36");
  });

  it("insets the layout gear from the canvas edge", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />));
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const wrapper = screen.getByLabelText("Graph layout settings").parentElement!;
    expect([...wrapper.classList]).toEqual(expect.arrayContaining(["absolute", "left-3", "top-3"]));
  });
});
describe("TaskGraph time filter (Task #1969)", () => {

  it("defaults to the 24h window", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />));
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    const sel = screen.getByLabelText<HTMLSelectElement>("Time window");
    expect(sel.value).toBe("24h");
    expect([...sel.options].map((o) => o.value)).toEqual(["24h", "7d", "30d", "all"]);
  });

  it("persists the window choice as a user setting", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />));
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    fireEvent.change(screen.getByLabelText("Time window"), { target: { value: "30d" } });
    expect(mockSetSettingCalls()).toContainEqual({ key: "display.task_window", value: "30d" });
  });

  it("falls back to 24h when the stored window is garbage", async () => {
    resetMockSettings({ "display.task_window": "banana" });
    useTasks.mockReturnValue(ok(sampleTasks()));
    render(wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />));
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.getByLabelText<HTMLSelectElement>("Time window").value).toBe("24h");
  });

  it("renders server-delivered out-of-window ancestors (ghost rows) dimmed in the graph", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      task(1815, { title: "old-parent", status: "done", parent_id: 1, ghost: true }),
      task(1848, { title: "child", status: "in_progress", parent_id: 1815 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />));
    await waitFor(() => expect(screen.getAllByText(/#1848/).length).toBeGreaterThan(0), { timeout: 4000 });
    const ghostG = screen.getAllByText(/#1815/)[0].closest("g")!;
    expect(ghostG.classList.contains("opacity-40")).toBe(true);
    expect(screen.getAllByText(/#1848/)[0].closest("g")!.classList.contains("opacity-40")).toBe(false);
  });

  it("hides ghost ancestors in the kanban (they are graph-only scaffolding)", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      task(1815, { title: "old-parent", status: "done", parent_id: 1, ghost: true }),
      task(1848, { title: "child", status: "in_progress", parent_id: 1815 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    render(wrapGraph(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />));
    await waitFor(() => expect(screen.getAllByText(/#1848/).length).toBeGreaterThan(0), { timeout: 4000 });
    fireEvent.click(screen.getByText("Kanban"));
    await screen.findByText(/child/);
    expect(screen.queryByText(/old-parent/)).toBeNull();
  });
});
