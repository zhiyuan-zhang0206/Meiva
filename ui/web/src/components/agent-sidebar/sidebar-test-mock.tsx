import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { vi } from "vitest";

import type { StatsWindowHours } from "@/lib/agents/sidebar";
import type { AgentRow, AlertsResponse, StatsDashboard, UiStatContribution } from "@/lib/contracts/types";
import { apiWrapper, fakeApi } from "@/lib/transport/tests/api/fake-api";

export const state = {
  agents: [] as AgentRow[],
  activeId: null as number | null,
  pendingActions: {} as Record<number, "restarting" | "terminating" | "resurrecting">,
  pendingSpawnCount: 0,
  isLoading: false,
  mobileSidebarOpen: false,
  sidebarCollapsed: false,
  stats: undefined as StatsDashboard | undefined,
  statsError: null as unknown,
  statsFetching: false,
  statsRefetch: vi.fn(),
  statsWindowHours: 24 as StatsWindowHours,
  pluginStatDeclarations: [] as UiStatContribution[],
  showTerminated: false,
  sidebarViewMode: "tree" as "tree" | "flat",
  searchQuery: "",
  userSettings: {} as Record<string, unknown>,
  setSetting: vi.fn(),
  setActiveId: vi.fn(),
  setMobileSidebarOpen: vi.fn(),
  setSidebarCollapsed: vi.fn(),
  setStatsWindowHours: vi.fn(),
  setSidebarViewMode: vi.fn(),
  setSearchQuery: vi.fn(),
};

export const pushSpy = vi.fn();

const sortListeners = new Set<() => void>();

export const bpMock = vi.fn(() => ({ tier: "xl", isNarrow: false, isLarge: true }));

export const sortStore = {
  value: null as null | { key: string; dir: "asc" | "desc" },
  subscribe: (cb: () => void) => {
    sortListeners.add(cb);
    return () => {
      sortListeners.delete(cb);
    };
  },
  set(next: { key: string; dir: "asc" | "desc" }) {
    this.value = next;
    for (const cb of sortListeners) cb();
  },
  reset() {
    this.value = null;
    sortListeners.clear();
  },
};

export function installLocalStoragePolyfill(): void {
  const store = new Map<string, string>();
  const fake: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, v),
    removeItem: (k) => store.delete(k),
    key: (i) => Array.from(store.keys())[i] ?? null,
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: fake,
    writable: true,
    configurable: true,
  });
}

export function StubAgentRow({
  agent,
  active,
  pending,
  wide,
  depth,
  onSelect,
  onTerminate,
  onRestart,
  onResurrect,
  onFork,
  onRename,
}: {
  agent: AgentRow;
  active: boolean;
  pending: string | undefined;
  wide: boolean;
  depth: number;
  onSelect: () => void;
  onTerminate: () => void;
  onRestart: () => void;
  onResurrect: (prompt?: string) => void;
  onFork: (prompt?: string) => void;
  onRename: (l: string) => void;
  onCompact: () => void;
}) {
  return (
    <li
      data-testid={`row-${agent.agent_id}`}
      data-active={active ? "1" : "0"}
      data-pending={pending ?? ""}
      data-wide={wide ? "1" : "0"}
      data-depth={depth}
    >
      <button data-testid={`select-${agent.agent_id}`} onClick={onSelect}>
        {agent.label ?? `#${agent.agent_id}`}
      </button>
      <button data-testid={`terminate-${agent.agent_id}`} onClick={onTerminate}>
        x
      </button>
      <button data-testid={`restart-${agent.agent_id}`} onClick={onRestart}>
        r
      </button>
      <button data-testid={`resurrect-${agent.agent_id}`} onClick={() => onResurrect()}>
        R
      </button>
      <button data-testid={`fork-${agent.agent_id}`} onClick={() => onFork()}>
        f
      </button>
      <button data-testid={`rename-${agent.agent_id}`} onClick={() => onRename("new-label")}>
        rn
      </button>
    </li>
  );
}

export function StubSpawnForm({ onSpawn }: { onSpawn: (machine?: string) => void }) {
  return (
    <button aria-label="Spawn agent" onClick={() => onSpawn("test-host")}>
      spawn
    </button>
  );
}

export function StubScrollArea({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div data-testid="scroll-area" className={className}>
      {children}
    </div>
  );
}

export function StubButton({
  children,
  onClick,
  "aria-label": ariaLabel,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  "aria-label"?: string;
  size?: string;
  variant?: string;
}) {
  return (
    <button onClick={onClick} aria-label={ariaLabel}>
      {children}
    </button>
  );
}

const NO_ALERTS: AlertsResponse = {
  alerts: [],
  meta: { window: "24h", total: 0, unresolved_count: 0 },
};

export const gatewayWrapper = apiWrapper(fakeApi({ getAlerts: () => Promise.resolve(NO_ALERTS) }));

export function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>, {
    wrapper: gatewayWrapper,
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

export const handlerFns = {
  onSpawn: vi.fn(),
  onTerminate: vi.fn(),
  onForceExpire: vi.fn(),
  onRestart: vi.fn(),
  onResurrect: vi.fn(),
  onFork: vi.fn(),
  onCompact: vi.fn(),
};

export const handlers = new Proxy(handlerFns, {
  get(target, prop, receiver): unknown {
    if (prop === "agents") return state.agents.filter((a) => a.status !== "terminated");
    if (prop === "ancestors") return state.agents.filter((a) => a.status === "terminated");
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
  pendingActions: Record<number, "restarting" | "terminating" | "resurrecting">;
  pendingSpawnCount: number;
  isLoading: boolean;
};

export function resetSidebarTestState(): void {
  pushSpy.mockReset();
  vi.clearAllMocks();
  installLocalStoragePolyfill();
  state.agents = [];
  state.activeId = null;
  state.pendingActions = {};
  state.pendingSpawnCount = 0;
  state.mobileSidebarOpen = false;
  state.sidebarCollapsed = false;
  state.stats = undefined;
  state.statsError = null;
  state.statsFetching = false;
  state.statsRefetch = vi.fn();
  state.statsWindowHours = 24;
  state.pluginStatDeclarations = [];
  state.showTerminated = false;
  state.sidebarViewMode = "tree";
  state.isLoading = false;
  state.searchQuery = "";
  state.userSettings = {};
  sortStore.reset();
  bpMock.mockReturnValue({ tier: "xl", isNarrow: false, isLarge: true });
}
