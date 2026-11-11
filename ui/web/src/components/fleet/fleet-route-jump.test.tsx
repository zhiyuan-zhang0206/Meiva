// meiva:note
// Fleet route jumps (task #2909): the deep links an inspector widget button produces. `?task=`
// selects a task, mounts the Tasks surface, and must not rewrite the durable view preference;
// malformed params are inert. The real surfaces and
// the real data hooks run here — a jump is asserted on what it puts on screen, not on a stub that
// echoes its props. The canvases themselves are covered by graph-view.test / task-graph.test.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.md

import { QueryClient } from "@tanstack/react-query";
import { cleanup, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AGENTS_QUERY_KEY } from "@/lib/agents/use-agents";
import {
  USER_SETTING_DEFAULTS,
  type AgentRow,
  type TaskRow,
} from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";

import { FleetView } from "./fleet-view";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";

// meiva:note
// The route hands its surfaces a `navigate`; a suite passes a spy, so a jump can be read
// without replacing `next/navigation`.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.md
const navigate = vi.fn();

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

const AGENTS: AgentRow[] = [
  {
    agent_id: 7,
    spawner: "user",
    harness: "meiva",
    fork_source_agent_id: null,
    status: "idling",
    spawned_at: "2026-06-06T00:00:00Z",
    last_active_at: "2026-06-06T00:00:00Z",
    last_inbound_at: "2026-06-06T00:00:00Z",
    label: "worker",
    machine: "test",
    supports_vision: true,
    llm_model: null, reasoning_effort: null,
    has_pending_question: false,
    has_unread_user_messages: false,
    heartbeat_paused_until: null,
    liveness_state: "online",
  },
];

const putSettingMock = vi.fn((key: string, value: unknown) =>
  Promise.resolve({ key, value, updated_at: "2026-06-14T00:00:00Z" }),
);

beforeEach(() => {
  resetTransport();
  transport.getSettings.mockImplementation(() => Promise.resolve({ settings: [] }));
  transport.putSetting.mockImplementation((key: string, value: unknown) => putSettingMock(key, value));
});

function task(over: Partial<TaskRow> & { id: number }): TaskRow {
  return {
    id: over.id,
    parent_id: over.parent_id ?? 1,
    title: over.title ?? `Task ${over.id}`,
    description: over.description ?? "",
    results: null,
    status: over.status ?? "in_progress",
    priority: over.priority ?? "P2",
    owner: over.owner ?? 7,
    created_by: "agent:7",
    created_at: over.created_at ?? "2026-06-14T00:00:00Z",
    updated_at: "2026-06-14T00:00:00Z",
    reminder_count: 0,
    last_reminded_at: null,
    remind_interval_seconds: null,
    owner_label: null,
  };
}

// meiva:note
// The test door for this file: the roster and the task registry a jump lands in
// enter through the caches the hooks read, in the shape the app stores; the transport answers the
// reads the surfaces still make, including an empty graph for the Agents tab.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.md
function wrap(
  ui: ReactElement,
  { agents = AGENTS, tasks = [] }: { agents?: AgentRow[]; tasks?: TaskRow[] } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS });
  qc.setQueryData(AGENTS_QUERY_KEY, { agents, ancestors: [] });
  // meiva:note
  // The board reads the graph projection of the 24h window and the 7d summary projection, so
  // both keys are seeded with the same rows.
  for (const window of ["24h", "7d", "30d", "all"] as const) {
    qc.setQueryData(["tasks", "graph", window], { tasks });
  }
  qc.setQueryData(["tasks", "summary", "7d"], { tasks });
  transport.getTasks.mockResolvedValue({ tasks });
  transport.getFleetGraph.mockResolvedValue({
    nodes: [],
    edges: [],
    telemetry_stale: false,
    snapshot_at: null,
  });
  return renderWithProviders(ui, { queryClient: qc, wallNow: () => Date.now() });
}

beforeEach(() => {
  window.innerWidth = 1024;
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
  window.history.replaceState(null, "", "/fleet");
  window.innerWidth = 1024;
});

describe("fleet route jumps", () => {
  it("?task= selects the task and mounts the Tasks surface without persisting the view", async () => {
    const tasks: TaskRow[] = [task({ id: 42, owner: 7 })];
    window.history.replaceState(null, "", "/fleet?task=42");
    wrap(<FleetView navigate={navigate} />, { tasks });
    // meiva:note
    // The route's task is selected on the real board: its node carries the selection ring. The pin
    // that lets this outrank an ambient agent selection (task #3500) is behavioural, and is covered
    // where the real board can show it (tests/task-graph-selection.test.tsx).
    await waitFor(() => {
      const node = screen.getAllByText(/#42/)[0].closest("g")!;
      expect(node.querySelector('[stroke-dasharray="4 3"]')).toBeTruthy();
    });
    expect(screen.queryByLabelText("Agent graph legend")).toBeNull();
    // The jump reveals the view but does not rewrite the durable preference.
    expect(putSettingMock).not.toHaveBeenCalledWith("display.fleet_left_view", expect.anything());
  });

  it("without ?task= the default view stays the agent graph", () => {
    wrap(<FleetView navigate={navigate} />);
    expect(screen.getByLabelText("Agent graph legend")).toBeTruthy();
    expect(screen.queryByLabelText("Task graph legend")).toBeNull();
  });

  it("?task= on mobile switches to the tasks tab", async () => {
    window.innerWidth = 375;
    const tasks: TaskRow[] = [task({ id: 42, owner: 7 })];
    window.history.replaceState(null, "", "/fleet?task=42");
    wrap(<FleetView navigate={navigate} />, { tasks });
    await waitFor(() => {
      const node = screen.getAllByText(/#42/)[0].closest("g")!;
      expect(node.querySelector('[stroke-dasharray="4 3"]')).toBeTruthy();
    });
    expect(screen.getByRole("tab", { name: /Tasks/, selected: true })).toBeTruthy();
  });

  it("malformed route ids are inert (default view, no crash)", () => {
    window.history.replaceState(null, "", "/fleet?task=abc");
    wrap(<FleetView navigate={navigate} />);
    expect(screen.getByLabelText("Agent graph legend")).toBeTruthy();
    expect(screen.queryByLabelText("Task graph legend")).toBeNull();
  });
});
