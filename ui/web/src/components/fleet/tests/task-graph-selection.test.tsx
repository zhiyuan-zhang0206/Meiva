
import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskRow } from "@/lib/contracts/types";
import type { TasksResult } from "@/lib/notifications/use-tasks";
import { resetMockSettings } from "@/test-support/user-settings-mock";

import { TaskGraph } from "../task-graph";
import { renderWithApi as render } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getTask.mockImplementation((id: number) => getTask(id));
});


// meiva:note
// The hover card reads its one row through TanStack Query, so the hover tests
// wrap their render in a client; the other tests never mount the card.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

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
  // meiva:note
  // Reset persisted graph/kanban mode + toggle state (now DB-backed user
  // settings) so a previous test's mode switch or toggle doesn't leak.
  // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
  resetMockSettings();
});

afterEach(cleanup);

describe("TaskGraph (graph mode)", () => {
  let harnessRenders = 0;
  function SelectionHarness() {
    const [agent, setAgent] = useState<number | null>(null);
    const [task, setTask] = useState<number | null>(null);
    harnessRenders += 1;
    if (harnessRenders > 60) {
      throw new Error("selection oscillation: harness re-rendered more than 60 times");
    }
    return (
      <div>
        <div data-testid="selection-state">{`${agent}:${task}`}</div>
        <button type="button" onClick={() => setAgent(9)}>
          external-select-agent-9
        </button>
        <TaskGraph
          selectedAgentId={agent}
          onSelectAgent={setAgent}
          selectedTaskId={task}
          onSelectTask={setTask}
        />
      </div>
    );
  }

  it("settles on a crossed selection instead of oscillating between two cards (task #3300)", async () => {
    resetMockSettings({ "display.task_graph_mode": "kanban" });
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(10, { title: "agent-7-work", parent_id: 1, owner: 7 }),
        task(20, { title: "agent-9-work", parent_id: 1, owner: 9 }),
      ]),
    );
    harnessRenders = 0;
    render(<SelectionHarness />);

    await waitFor(() => expect(screen.getAllByText(/#20/).length).toBeGreaterThan(0), { timeout: 4000 });

    fireEvent.click(screen.getByText("external-select-agent-9"));
    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("9:20"));

    fireEvent.click(screen.getByText(/#10/));
    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("7:10"));

    fireEvent.click(screen.getByText("external-select-agent-9"));

    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("9:20"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.getByTestId("selection-state").textContent).toBe("9:20");
    expect(harnessRenders).toBeLessThan(30);
    const solidCard = screen.getByText(/#20/).closest('[role="button"]');
    expect(solidCard?.className).toContain("border-sky-400");
    expect(solidCard?.className).not.toContain("border-sky-400/40");
  });

  it("converges when mounted with a crossed selection (the Graph-tab swap path, task #3300)", async () => {
    resetMockSettings({ "display.task_graph_mode": "kanban" });
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(10, { title: "agent-7-work", parent_id: 1, owner: 7 }),
        task(20, { title: "agent-9-work", parent_id: 1, owner: 9 }),
      ]),
    );

    function CrossedMountHarness() {
      const [boardMounted, setBoardMounted] = useState(false);
      const [agent, setAgent] = useState<number | null>(9);
      const [task, setTask] = useState<number | null>(10);
      harnessRenders += 1;
      if (harnessRenders > 60) {
        throw new Error("selection oscillation: harness re-rendered more than 60 times");
      }
      return (
        <div>
          <div data-testid="selection-state">{`${agent}:${task}`}</div>
          <button type="button" onClick={() => setBoardMounted(true)}>
            scratch-mount
          </button>
          {boardMounted ? (
            <TaskGraph
              selectedAgentId={agent}
              onSelectAgent={setAgent}
              selectedTaskId={task}
              onSelectTask={setTask}
            />
          ) : null}
        </div>
      );
    }

    harnessRenders = 0;
    render(<CrossedMountHarness />);
    fireEvent.click(screen.getByText("scratch-mount"));

    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("9:20"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.getByTestId("selection-state").textContent).toBe("9:20");
    expect(harnessRenders).toBeLessThan(30);
  });

  function ColdListHarness() {
    const [boardMounted, setBoardMounted] = useState(false);
    const [agent, setAgent] = useState<number | null>(9);
    const [task, setTask] = useState<number | null>(10);
    harnessRenders += 1;
    if (harnessRenders > 60) {
      throw new Error("selection oscillation: harness re-rendered more than 60 times");
    }
    return (
      <div>
        <div data-testid="selection-state">{`${agent}:${task}`}</div>
        <button type="button" onClick={() => setBoardMounted(true)}>
          scratch-mount
        </button>
        {boardMounted ? (
          <TaskGraph
            selectedAgentId={agent}
            onSelectAgent={setAgent}
            selectedTaskId={task}
            onSelectTask={setTask}
          />
        ) : null}
      </div>
    );
  }

  it("converges when a crossed pair mounts while the task list is cold (kanban, QA #6177 round 2)", async () => {
    resetMockSettings({ "display.task_graph_mode": "kanban" });
    useTasks.mockReturnValue(ok([]));

    harnessRenders = 0;
    const view = render(<ColdListHarness />);
    fireEvent.click(screen.getByText("scratch-mount"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(10, { title: "agent-7-work", parent_id: 1, owner: 7 }),
        task(20, { title: "agent-9-work", parent_id: 1, owner: 9 }),
      ]),
    );
    view.rerender(<ColdListHarness />);

    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("9:20"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.getByTestId("selection-state").textContent).toBe("9:20");
    expect(harnessRenders).toBeLessThan(30);
  });

  it("converges when a crossed pair mounts while the task list is cold (graph, QA #6177 round 2)", async () => {
    resetMockSettings({ "display.task_graph_mode": "graph" });
    useTasks.mockReturnValue(ok([]));

    harnessRenders = 0;
    const view = render(<ColdListHarness />);
    fireEvent.click(screen.getByText("scratch-mount"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(10, { title: "agent-7-work", parent_id: 1, owner: 7 }),
        task(20, { title: "agent-9-work", parent_id: 1, owner: 9 }),
      ]),
    );
    view.rerender(<ColdListHarness />);

    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("9:20"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.getByTestId("selection-state").textContent).toBe("9:20");
    expect(harnessRenders).toBeLessThan(30);
  });

  it("settles after one handler writes a crossed pair in a single commit (QA #6177 boundary)", async () => {
    resetMockSettings({ "display.task_graph_mode": "kanban" });
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(10, { title: "agent-7-work", parent_id: 1, owner: 7 }),
        task(20, { title: "agent-9-work", parent_id: 1, owner: 9 }),
      ]),
    );

    function CrossedWriteHarness() {
      const [agent, setAgent] = useState<number | null>(null);
      const [task, setTask] = useState<number | null>(null);
      harnessRenders += 1;
      if (harnessRenders > 60) {
        throw new Error("selection oscillation: harness re-rendered more than 60 times");
      }
      return (
        <div>
          <div data-testid="selection-state">{`${agent}:${task}`}</div>
          <button
            type="button"
            onClick={() => {
              setAgent(9);
              setTask(10);
            }}
          >
            scratch-cross
          </button>
          <TaskGraph
            selectedAgentId={agent}
            onSelectAgent={setAgent}
            selectedTaskId={task}
            onSelectTask={setTask}
          />
        </div>
      );
    }

    harnessRenders = 0;
    render(<CrossedWriteHarness />);
    fireEvent.click(screen.getByText("scratch-cross"));

    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("9:20"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.getByTestId("selection-state").textContent).toBe("9:20");
    expect(harnessRenders).toBeLessThan(30);
  });

  function RouteCarriedHarness() {
    const [agent, setAgent] = useState<number | null>(null);
    const [task, setTask] = useState<number | null>(2);
    harnessRenders += 1;
    if (harnessRenders > 60) {
      throw new Error("selection oscillation: harness re-rendered more than 60 times");
    }
    return (
      <div>
        <div data-testid="selection-state">{`${agent}:${task}`}</div>
        <button type="button" onClick={() => setAgent(9)}>
          external-select-agent-9
        </button>
        <TaskGraph
          selectedAgentId={agent}
          onSelectAgent={setAgent}
          selectedTaskId={task}
          routeTaskId={2}
          onSelectTask={setTask}
        />
      </div>
    );
  }

  it("keeps a route-carried task when an agent is already selected — the jump outranks the Inbox's default row (task #3500)", async () => {
    const onSelectTask = vi.fn();
    const onSelectAgent = vi.fn();
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(2, { title: "jumped-into", parent_id: 1, owner: 7 }),
        task(20, { title: "agent-9-work", parent_id: 1, owner: 9 }),
      ]),
    );
    render(
      <TaskGraph
        selectedAgentId={9}
        onSelectAgent={onSelectAgent}
        selectedTaskId={2}
        routeTaskId={2}
        onSelectTask={onSelectTask}
      />,
    );

    await waitFor(() => expect(onSelectAgent).toHaveBeenCalledWith(7), { timeout: 2000 });
    expect(onSelectTask).not.toHaveBeenCalled();
  });

  it("keeps a route-carried task pinned when an agent-only write lands after the jump settled (task #3500)", async () => {
    resetMockSettings({ "display.task_graph_mode": "kanban" });
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(2, { title: "jumped-into", parent_id: 1, owner: 7 }),
        task(20, { title: "agent-9-work", parent_id: 1, owner: 9 }),
      ]),
    );
    harnessRenders = 0;
    render(<RouteCarriedHarness />);

    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("7:2"), { timeout: 4000 });

    fireEvent.click(screen.getByText("external-select-agent-9"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.getByTestId("selection-state").textContent).toBe("9:2");
    expect(harnessRenders).toBeLessThan(30);
  });

  it("releases the route pin once the task selection moves off the route (task #3500)", async () => {
    resetMockSettings({ "display.task_graph_mode": "kanban" });
    useTasks.mockReturnValue(
      ok([
        task(1, { title: "root", status: "in_progress" }),
        task(2, { title: "jumped-into", parent_id: 1, owner: 7 }),
        task(20, { title: "agent-9-work", parent_id: 1, owner: 9 }),
        task(30, { title: "agent-11-work", parent_id: 1, owner: 11 }),
      ]),
    );
    harnessRenders = 0;
    render(<RouteCarriedHarness />);
    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("7:2"), { timeout: 4000 });
    await waitFor(() => expect(screen.getAllByText(/#30/).length).toBeGreaterThan(0), { timeout: 4000 });

    fireEvent.click(screen.getAllByText(/#30/)[0]);
    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("11:30"));

    fireEvent.click(screen.getByText("external-select-agent-9"));
    await waitFor(() => expect(screen.getByTestId("selection-state").textContent).toBe("9:20"));
  });

  it("renders a static selection ring — no perpetual pulse on the selected node (task #3278)", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={2} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => {
      expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0);
    }, { timeout: 4000 });
    await new Promise((r) => setTimeout(r, 0));
    const rings = Array.from(container.querySelectorAll("circle,rect")).filter(
      (el) => el.getAttribute("stroke-dasharray") === "4 3",
    );
    expect(rings.length).toBeGreaterThan(0);
    for (const ring of rings) {
      const cls = ring.getAttribute("class") ?? "";
      expect(cls).toContain("text-sky-400");
      expect(cls).not.toContain("animate-pulse");
    }
  });

  it("renders parent-child edges hanging under the root node", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(<TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText(/#1/).length).toBeGreaterThan(0), { timeout: 4000 });

    const mainSvg = container.querySelector("svg[role='img']");
    const edgeLines = mainSvg ? mainSvg.querySelectorAll("g > line") : [];
    expect(edgeLines.length).toBe(2);
  });

  it("renders a toggle-hidden done parent as a ghost node so its child is not a fake orphan (#1848)", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      task(1815, { title: "done-parent", status: "done", parent_id: 1 }),
      task(1848, { title: "child", status: "in_progress", parent_id: 1815 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#1848/).length).toBeGreaterThan(0), { timeout: 4000 });

    const ghostTexts = screen.getAllByText(/#1815/);
    expect(ghostTexts.length).toBeGreaterThan(0);
    const ghostGroup = ghostTexts[0].closest("g")!;
    expect(ghostGroup.classList.contains("opacity-40")).toBe(true);
    const childGroup = screen.getAllByText(/#1848/)[0].closest("g")!;
    expect(childGroup.classList.contains("opacity-40")).toBe(false);

    const mainSvg = container.querySelector("svg[role='img']");
    const edgeLines = mainSvg ? mainSvg.querySelectorAll("g > line") : [];
    expect(edgeLines.length).toBe(2);
  });

  it("walks a chain of hidden ancestors so a deep subtree stays connected", async () => {
    const tasks: TaskRow[] = [
      task(1, { title: "root", status: "in_progress" }),
      task(10, { title: "g-cancelled", status: "cancelled", parent_id: 1 }),
      task(11, { title: "g-done", status: "done", parent_id: 10 }),
      task(12, { title: "leaf", status: "in_progress", parent_id: 11 }),
    ];
    useTasks.mockReturnValue(ok(tasks));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#12/).length).toBeGreaterThan(0), { timeout: 4000 });
    for (const id of [10, 11]) {
      const g = screen.getAllByText(new RegExp(`#${String(id)}`))[0].closest("g")!;
      expect(g.classList.contains("opacity-40")).toBe(true);
    }
    const mainSvg = container.querySelector("svg[role='img']");
    expect(mainSvg!.querySelectorAll("g > line").length).toBe(3);
  });

  it("does not ghost a hidden done task with no visible children", async () => {
    useTasks.mockReturnValue(ok(sampleTasks()));
    const { container } = render(
      <TaskGraph selectedAgentId={null} onSelectAgent={vi.fn()} selectedTaskId={null} onSelectTask={vi.fn()} />,
    );
    await waitFor(() => expect(screen.getAllByText(/#2/).length).toBeGreaterThan(0), { timeout: 4000 });
    expect(screen.queryByText(/#4/)).toBeNull();
    expect(screen.queryByText(/#5/)).toBeNull();
    const mainSvg = container.querySelector("svg[role='img']");
    expect(mainSvg!.querySelectorAll("g.opacity-40").length).toBe(0);
  });
});
