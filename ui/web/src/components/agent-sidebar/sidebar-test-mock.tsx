// meiva:note
// The sidebar tests' scenario: the props the page hands the rail, the values the
// environment answers with (settings, the page's media, the gateway's stats and plugin
// declarations), and nothing that replaces a module. `wrap` composes the app's real
// providers -- browser, transport, query cache -- and seeds each value on the owner that
// holds it in production: settings in the ["user-settings"] cache, the drawer/search
// selection in the zustand store, stats and declarations on the gateway answers, the
// viewport on the fake browser. A write (`setSetting`) therefore lands on
// `transport.putSetting`, which is where a user's click is asserted.
import { QueryClient } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { vi } from "vitest";

import { fireEvent, screen } from "@testing-library/react";
import type {
  AgentLineage,
  AlertsResponse,
  AgentRow,
  ModelsResponse,
  StatsDashboard,
  SystemStatus,
  UiStatContribution,
} from "@/lib/contracts/types";
import type { StatsWindowHours } from "@/lib/agents/sidebar";
import { SIDEBAR_SORT_DEFAULT } from "@/lib/agents/sidebar";
import { BREAKPOINT_LG_PX, BREAKPOINT_MD_PX, BREAKPOINT_WIDE_PX } from "@/lib/layout/breakpoint";
import { useStore } from "@/lib/state/store";
import { fakeBrowser } from "@/test-support/browser";
import { renderWithProviders } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";

// meiva:note
// The instant the page reports as "now". Fixed, so a surface that formats a duration against
// the clock renders the same text on every run.
export const SCENARIO_NOW = Date.parse("2026-05-20T00:00:00Z");

// meiva:note
// The queries the breakpoint hook asks, spelled from the one place their widths are defined, so
// a breakpoint change moves the scenarios with it instead of silently leaving them behind.
function tierQuery(width: number): string {
  return `(min-width: ${width}px)`;
}

// meiva:note
// The page a desktop rail reads: md/lg/wide all match.
const WIDE_MEDIA = {
  [tierQuery(BREAKPOINT_MD_PX)]: true,
  [tierQuery(BREAKPOINT_LG_PX)]: true,
  [tierQuery(BREAKPOINT_WIDE_PX)]: true,
};

// meiva:note
// The page a phone reads: none of the desktop tiers match, so the drawer mounts.
export const NARROW_MEDIA = {
  [tierQuery(BREAKPOINT_MD_PX)]: false,
  [tierQuery(BREAKPOINT_LG_PX)]: false,
  [tierQuery(BREAKPOINT_WIDE_PX)]: false,
};

// meiva:note
// The scenario a sidebar test sets up: first the props the page hands the rail, then the
// values the environment answers with (preferences, viewport, stats, plugin declarations).
export const state = {
  agents: [] as AgentRow[],
  activeId: null as number | null,
  pendingActions: {} as Record<number, "restarting" | "terminating" | "resurrecting">,
  pendingSpawnCount: 0,
  isLoading: false,
  mobileSidebarOpen: false,
  sidebarCollapsed: false,
  sidebarViewMode: "tree" as "tree" | "flat",
  showTerminated: false,
  statsWindowHours: 24 as StatsWindowHours,
  sort: SIDEBAR_SORT_DEFAULT,
  searchQuery: "",
  userSettings: {} as Record<string, unknown>,
  media: WIDE_MEDIA as Record<string, boolean>,
  stats: undefined as StatsDashboard | undefined,
  statsError: null as unknown,
  statsFetching: false,
  pluginStatDeclarations: [] as UiStatContribution[],
};

// meiva:note
// Every persistent preference the sidebar reads, as the cache holds them.
function scenarioSettings(): Record<string, unknown> {
  return {
    "display.sidebar_collapsed": state.sidebarCollapsed,
    "display.sidebar_view_mode": state.sidebarViewMode,
    "display.show_terminated": state.showTerminated,
    "display.stats_window_hours": state.statsWindowHours,
    "display.sidebar_sort": state.sort,
    ...state.userSettings,
  };
}

function errorText(value: unknown): string {
  return value === null || value === undefined ? "stats read failed" : JSON.stringify(value);
}

// meiva:note
// What the gateway answers for the stats dashboard: data, a pending read, or a failure.
function statsAnswer(): () => Promise<StatsDashboard> {
  if (state.statsError !== null) {
    const failure = state.statsError instanceof Error ? state.statsError : new Error(errorText(state.statsError));
    // meiva:note
    // A scenario with data and an error is "the first read answered, a later one failed":
    // the cache keeps the values and the hook hands both halves to the panel.
    if (state.stats !== undefined) {
      let reads = 0;
      const first = state.stats;
      return () => {
        reads += 1;
        return reads === 1 ? Promise.resolve(first) : Promise.reject(failure);
      };
    }
    return () => Promise.reject(failure);
  }
  if (state.statsFetching) {
    return () => new Promise<StatsDashboard>(() => undefined);
  }
  const stats = state.stats;
  // meiva:note
  // No data and no error is "not loaded yet": a read that never settles, not a failure --
  // a rejected read would render the error state the tests reserve for `statsError`.
  return () => (stats === undefined ? new Promise<StatsDashboard>(() => undefined) : Promise.resolve(stats));
}

// meiva:note
// The router the framework mock records navigation calls on.
export const pushSpy = vi.fn();

const NO_ALERTS: AlertsResponse = {
  alerts: [],
  meta: { window: "24h", total: 0, unresolved_count: 0 },
};

export function wrap(ui: ReactElement) {
  const settings = scenarioSettings();
  // meiva:note
  // The gateway is a small settings store: it answers what the cache was seeded with, and a
  // write (`putSetting`) is remembered, so the refetch that follows a write re-reads the value
  // the user chose instead of quietly restoring the scenario's defaults.
  const stored = new Map<string, unknown>(Object.entries(settings));
  transport.getSettings.mockImplementation(() =>
    Promise.resolve({
      settings: [...stored].map(([key, value]) => ({ key, value })),
    }),
  );
  transport.putSetting.mockImplementation((key: string, value: unknown) => {
    stored.set(key, value);
    return Promise.resolve({ key, value });
  });
  transport.getStatsDashboard.mockImplementation(statsAnswer());
  transport.getUiContributions.mockResolvedValue({
    themes: [],
    nav: [],
    stats: state.pluginStatDeclarations,
  });
  // meiva:note
  // The create popover mounts the real spawn form; it asks the gateway which hosts can run an
  // agent and which models they offer.
  transport.getSystemStatus.mockResolvedValue(spawnableStatus());
  transport.getModels.mockResolvedValue(defaultModels());
  transport.getHarnessAvailability.mockResolvedValue([]);
  useStore.setState({
    activeId: state.activeId,
    mobileSidebarOpen: state.mobileSidebarOpen,
    searchQuery: state.searchQuery,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 }, mutations: { retry: false } },
  });
  queryClient.setQueryData(["alerts"], NO_ALERTS);
  return renderWithProviders(ui, {
    browser: fakeBrowser({ media: state.media }),
    settings,
    queryClient,
    wallNow: () => SCENARIO_NOW,
  });
}

export function makeAgent(overrides: Partial<AgentRow>): AgentRow {
  return {
    agent_id: 1,
    spawner: "user",
    harness: "meiva",
    fork_source_agent_id: null,
    status: "idling",
    spawned_at: "2026-05-15T00:00:00Z",
    last_active_at: "2026-05-15T00:00:00Z",
    last_inbound_at: "2026-05-15T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    awaiting_response_count: 0,
    highest_notice_priority: null,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    ...overrides,
  };
}

function lineageOf(agents: readonly AgentRow[]): AgentLineage[] {
  return agents
    .filter((agent) => agent.status === "terminated")
    .map(({ agent_id, spawner, fork_source_agent_id }) => ({ agent_id, spawner, fork_source_agent_id }));
}

const handlerFns = {
  onSpawn: vi.fn(),
  onTerminate: vi.fn(),
  onForceExpire: vi.fn(),
  onRestart: vi.fn(),
  onResurrect: vi.fn(),
  onFork: vi.fn(),
  onCompact: vi.fn(),
  onRename: vi.fn(),
};

export const handlers = new Proxy(handlerFns, {
  get(target, prop, receiver): unknown {
    if (prop === "agents") return state.agents.filter((a) => a.status !== "terminated");
    if (prop === "ancestors") return lineageOf(state.agents);
    if (prop === "pendingActions") return state.pendingActions;
    if (prop === "pendingSpawnCount") return state.pendingSpawnCount;
    if (prop === "isLoading") return state.isLoading;
    return Reflect.get(target, prop, receiver) as unknown;
  },
  ownKeys() {
    return [
      ...Object.keys(handlerFns),
      "agents",
      "ancestors",
      "pendingActions",
      "pendingSpawnCount",
      "isLoading",
    ];
  },
  getOwnPropertyDescriptor() {
    return { enumerable: true, configurable: true };
  },
}) as typeof handlerFns & {
  agents: AgentRow[];
  ancestors: AgentLineage[];
  pendingActions: Record<number, "restarting" | "terminating" | "resurrecting">;
  pendingSpawnCount: number;
  isLoading: boolean;
};

export function resetSidebarTestState(): void {
  resetTransport();
  // meiva:note
  // The sidebar reads its roster and its label writes through the transport; the archive
  // list is the terminated half of the state the tests push in.
  transport.patchAgentLabel.mockResolvedValue(undefined);
  transport.listAgents.mockImplementation(() =>
    Promise.resolve({
      agents: state.agents.filter((a) => a.status === "terminated"),
      next_cursor: null,
    }),
  );
  vi.clearAllMocks();
  pushSpy.mockReset();
  state.agents = [];
  state.activeId = null;
  state.pendingActions = {};
  state.pendingSpawnCount = 0;
  state.mobileSidebarOpen = false;
  state.sidebarCollapsed = false;
  state.sidebarViewMode = "tree";
  state.showTerminated = false;
  state.statsWindowHours = 24;
  state.sort = SIDEBAR_SORT_DEFAULT;
  state.searchQuery = "";
  state.userSettings = {};
  state.media = WIDE_MEDIA;
  state.stats = undefined;
  state.statsError = null;
  state.statsFetching = false;
  state.pluginStatDeclarations = [];
  state.isLoading = false;
  useStore.setState({ activeId: null, mobileSidebarOpen: false, searchQuery: "" });
}

// meiva:note
// Radix menus reach for pointer capture and scrolling that happy-dom does not implement.
export function installMenuPolyfills(): void {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => undefined;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
}

// meiva:note
// The row a page renders for one agent: its own button, and the list item around it.
export function rowButton(agentId: number): HTMLElement {
  // meiva:note
  // A row shows `#id` alone or `#id · label`; both put the id in the row's own button.
  const label =
    screen.queryByText(`#${agentId}`, { exact: true }) ??
    screen.getByText(new RegExp(`^#${agentId} · `));
  const button = label.closest("button");
  if (button === null) throw new Error(`row #${agentId} is not inside a button`);
  return button;
}

export function rowItem(agentId: number): HTMLElement {
  const item = rowButton(agentId).closest("li");
  if (item === null) throw new Error(`row #${agentId} is not inside a list item`);
  return item;
}

// meiva:note
// The row's right-click menu, the only place its lifecycle actions live.
export function openRowMenu(agentId: number): void {
  fireEvent.contextMenu(rowItem(agentId));
}

// meiva:note
// A row's label span (`#12` or `#12 · label`), or null when the page renders no such row.
export function queryRow(agentId: number): HTMLElement | null {
  return screen.queryByText(new RegExp(`^#${agentId}(?:$| · )`));
}

// meiva:note
// The ids of the rows on screen, in the order the page lays them out.
export function rowOrder(): string[] {
  return screen.queryAllByText(/^#\d+(?:$| · )/).map((label) => {
    const text = label.textContent;
    const id = /^#(\d+)/.exec(text);
    if (id === null) throw new Error("row label without an id");
    return id[1];
  });
}

// meiva:note
// Which tree guides the row drew: one per ancestor level, deepest last.
export function treeGuideDepths(agentId: number): number[] {
  return Array.from(rowItem(agentId).querySelectorAll('[data-testid="tree-guide"]')).map((guide) =>
    Number(guide.getAttribute("data-depth")),
  );
}

function clusterMachine(name: string, current: string) {
  return {
    name,
    serve_gateway: name === current,
    serve_agent_runner: name !== current,
    serve_observability_station: false,
    identity_mismatch: false,
    is_staging: false,
    gateway_url: `http://${name}:8000`,
    up_since_at: "2026-05-19T00:00:00Z",
    online: true,
    paused: false,
    agent_host_online: true,
    shell_count: 0,
    agent_count: 0,
    session_count: 0,
    agent_groups: [],
    resource: null,
  };
}

// meiva:note
// A cluster with one spawnable host, which is what the create form needs to offer Spawn.
function spawnableStatus(): SystemStatus {
  return {
    cluster: {
      current_machine: "cloud",
      current_serve_gateway: true,
      current_serve_agent_runner: false,
      current_serve_observability_station: false,
      current_paused: false,
      machines: [clusterMachine("cloud", "cloud"), clusterMachine("test-host", "cloud")],
    },
    services: { items: [] },
  };
}

function defaultModels(): ModelsResponse {
  return {
    providers: { deepseek: ["deepseek-v4-pro"] },
    models: {
      "deepseek-v4-pro": {
        provider: "deepseek",
        harnesses: ["meiva"],
        context_window: 128_000,
        reasoning_effort_options: ["high", "max"],
        reasoning_effort_default: "max",
        pricing: { input: 1.65, cache_read: 0.14, output: 6.6 },
      },
    },
    default: "deepseek-v4-pro",
  };
}
