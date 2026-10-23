import { notifyManager, QueryClient, QueryClientProvider, type DefaultOptions } from "@tanstack/react-query";
import { cleanup, render as rtlRender } from "@testing-library/react";
import { vi } from "vitest";

import { projectAgentStatus, USER_SETTING_DEFAULTS } from "@/lib/contracts/types";
import type {
  AgentInspectLive,
  AgentInspectStatistics,
  AgentRoster,
  AgentRow,
  InspectWidget,
  PageRow,
  WireAgentCard,
} from "@/lib/contracts/types";
import { useStore } from "@/lib/state/store";

const SETTINGS_QUERY_KEY = ["user-settings"] as const;

notifyManager.setScheduler((cb) => {
  cb();
});

export const getAgentInspectStatistics =
  vi.fn<
    (
      agentId: number,
      hours?: number | null,
      signal?: AbortSignal,
    ) => Promise<AgentInspectStatistics>
  >();
export const getAgentInspectLive =
  vi.fn<(agentId: number, signal?: AbortSignal) => Promise<AgentInspectLive>>();
export const getAgentRoster = vi.fn<(signal?: AbortSignal) => Promise<AgentRoster>>();
export const getAgent = vi.fn<(id: number, signal?: AbortSignal) => Promise<AgentRow>>();
export const getAgentInspectWidgets = vi.fn<
  (agentId: number, signal?: AbortSignal) => Promise<InspectWidget[]>
>(() => Promise.resolve([]));
export const listPages = vi.fn<(agentId: number) => Promise<PageRow[]>>(() =>
  Promise.resolve([]),
);
export const resolveNotice = vi.fn(() => Promise.resolve({ status: "ok" }));
export const putSetting = vi.fn((key: string, value: unknown) =>
  Promise.resolve({ key, value, updated_at: "2026-06-14T12:00:00Z" }),
);
export const getSettings = vi.fn(() =>
  Promise.resolve({
    settings: [
      {
        key: "display.inspector_open",
        value: panelState.open,
        updated_at: "2026-06-14T12:00:00Z",
      },
    ],
  }),
);

export const streamHandlers = {
  system: undefined as ((event: unknown) => void) | undefined,
  connection: undefined as ((event: { type: string }) => void) | undefined,
};

export const panelState = { open: true, hours: 24 as number | null };

export function setInspectorOpen(open: boolean, qc?: QueryClient) {
  panelState.open = open;
  useStore.getState().setMobileInspectorOpen(open);
  qc?.setQueryData(SETTINGS_QUERY_KEY, {
    ...USER_SETTING_DEFAULTS,
    "display.inspector_open": open,
  });
}

export function setInspectorHours(hours: number | null) {
  panelState.hours = hours;
  useStore.getState().setInspectorHours(hours);
}

export function createInspectorQueryClient(
  defaultOptions: DefaultOptions = { queries: { retry: false } },
): QueryClient {
  const qc = new QueryClient({ defaultOptions });
  qc.setQueryData(SETTINGS_QUERY_KEY, {
    ...USER_SETTING_DEFAULTS,
    "display.inspector_open": panelState.open,
  });
  return qc;
}

export function setupInspectorTest() {
  window.innerWidth = 1024;
  setInspectorOpen(true);
  setInspectorHours(24);
  getAgentInspectStatistics.mockResolvedValue(fixture());
  getAgentInspectLive.mockResolvedValue(liveFixture());
  getAgentInspectWidgets.mockResolvedValue([]);
  getAgentRoster.mockResolvedValue({ agents: [], ancestors: [] });
}

export function cleanupInspectorTest() {
  vi.useRealTimers();
  cleanup();
  getAgentInspectStatistics.mockReset();
  getAgentInspectLive.mockReset();
  getAgentInspectWidgets.mockReset();
  getAgentRoster.mockReset();
  getAgent.mockReset();
  resolveNotice.mockClear();
  putSetting.mockClear();
  getSettings.mockClear();
  window.innerWidth = 1024;
  setInspectorOpen(true);
  setInspectorHours(24);
  streamHandlers.system = undefined;
  streamHandlers.connection = undefined;
}

export function render(
  ui: React.ReactElement,
  qc = createInspectorQueryClient(),
) {
  if (qc.getQueryData(SETTINGS_QUERY_KEY) === undefined) {
    qc.setQueryData(SETTINGS_QUERY_KEY, {
      ...USER_SETTING_DEFAULTS,
      "display.inspector_open": panelState.open,
    });
  }
  useStore.getState().setMobileInspectorOpen(panelState.open);
  useStore.getState().setInspectorHours(panelState.hours);
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

export function renderWithGlobalRetries(ui: React.ReactElement) {
  const qc = createInspectorQueryClient({
    queries: { retry: 3, retryDelay: 0 },
  });
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

export function useFrozenClock(iso: string) {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(iso));
}

export function fixture(overrides: Partial<AgentInspectStatistics> = {}): AgentInspectStatistics {
  return {
    agent_id: 1,
    window_hours: 24,
    metadata: {
      collection: "observed",
      window_start: null,
      window_end: "2026-09-17T00:00:00Z",
      sampled_at: "2026-09-17T00:00:00Z",
      collection_started_at: "2026-09-01T00:00:00Z",
      last_observed_at: "2026-09-17T00:00:00Z",
      cost: { availability: "observed", sources: ["observations"] },
      turns: {
        availability: "observed",
        sources: ["observations"],
        duration_precision: "exact",
      },
      activity: { availability: "observed", sources: ["observations"] },
      lifecycle: { availability: "observed", sources: ["state_transitions"] },
    },
    cost: {
      cost_usd: 0.4213,
      unpriced_calls: 1,
      llm_calls: 142,
      tokens_in: 1_200_000,
      tokens_out: 84_000,
      tokens_cached: 1_100_000,
      tokens_reasoning: 5_000,
      cache_hit_pct: 91.7,
    },
    stats: {
      turn_total: 7,
      turn_ok: 6,
      turn_p50_seconds: 3.1,
      turn_p90_seconds: 9.4,
      turn_min_seconds: 1.2,
      turn_max_seconds: 41,
      exec_ok: 51,
      exec_failed: 2,
    },
    tps: { lm_stage_tps: 42.5, agent_lifecycle_tps: 8.3 },
    activity: { active_seconds: 1800, alive_seconds: 3600, active_rate: 0.5, llm_seconds: 1200, exec_seconds: 450 },
    ...overrides,
  };
}


export function liveFixture(overrides: Partial<AgentInspectLive> = {}): AgentInspectLive {
  return {
    agent_id: 1,
    machine: "test-host",
    status: "running",
    liveness_state: "online",
    last_probe_at: null,
    shells_available: true,
    shells: [
      {
        id: 0,
        name: "dev-server",
        created_at: new Date(Date.now() - 8040_000).toISOString(),
        uptime_seconds: 8040,
      },
      { id: 1, name: null, created_at: null, uptime_seconds: 45 },
      {
        id: 2,
        name: "watcher",
        created_at: new Date(Date.now() - 660_000).toISOString(),
        uptime_seconds: 660,
      },
    ],
    config_overlay: { llm_model: "claude-opus-4-8", auto_compact_fraction: 0.7 },
    heartbeat: { interval_s: 300, next_at: null, paused_until: null, heartbeat_pending: false, last_pause: null },
    notice: null,
    spawned_at: "2026-06-14T12:00:00Z",
    ...overrides,
  };
}

export function rosterFixture(overrides: Partial<WireAgentCard> = {}): AgentRoster {
  return {
    agents: [projectAgentStatus({
      agent_id: 1,
      spawner: "user",
      harness: "meiva",
      fork_source_agent_id: null,
      status: "idling",
      spawned_at: "2026-06-14T12:00:00Z",
      last_active_at: "2026-06-14T12:00:05Z",
      last_inbound_at: "2026-06-14T12:00:05Z",
      label: null,
      machine: "test-host",
      supports_vision: false,
      liveness_state: "online",
      observation: { runtime_owner: "unknown" },
      awaiting_response_count: 0,
      highest_notice_priority: null,
      unread_notice_count: 0,
      heartbeat_paused_until: null,
      open_impersonation_session_id: 7,
      open_impersonation_status: "active",
      ...overrides,
    })],
    ancestors: [],
  };
}

export function widgetFixture(over: Partial<InspectWidget> = {}): InspectWidget {
  return {
    plugin: "meiva_fleet",
    id: "today-tasks",
    kind: "taskList",
    order: 250,
    title: null,
    tasks: [
      { id: 42, title: "Ship the inspector fix", priority: "P1" },
      { id: 43, title: "Reply to QA", priority: "P2" },
    ],
    ...over,
  };
}

export const widgetNotice = {
  id: 92,
  title: "Needs a call",
  content: null,
  priority: "P1" as const,
  require_response: true,
  blocking: false,
  created_at: "2026-06-14T12:00:00Z",
};
