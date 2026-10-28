

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { vi } from "vitest";

import type {
  AgentMachineRow,
  ConfigView,
  ConfigWriteResult,
  ModelsResponse,
  ResolvedConfigView,
  SystemStatus,
} from "@/lib/contracts/types";

import ConfigPage from "./page";
import { resetTransport, transport } from "@/test-support/fake-api";
import { ApiProvider } from "@/lib/transport/transport-context";

const localStorageMock = (() => {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
    clear: () => store.clear(),
  };
})();
Object.defineProperty(globalThis, "localStorage", {
  value: localStorageMock,
  writable: true,
});


export function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

// meiva:note
// The page reads its transport from <ApiProvider>, so the harness hands it the fake
// the tests answer. Same provider the app mounts -- nothing replaces the module.
export function wrap(ui: React.ReactElement, qc?: QueryClient) {
  const client = qc ?? makeQc();
  return render(
    <QueryClientProvider client={client}>
      <ApiProvider api={transport}>{ui}</ApiProvider>
    </QueryClientProvider>,
  );
}

export async function renderSettled() {
  const rendered = wrap(<ConfigPage />);
  await screen.findByText("LOG LEVEL");

  await screen.findByTestId("per-model-source-auto_compact_fraction");
  return rendered;
}

export function inGroups(text: string): HTMLElement[] {
  return screen
    .getAllByTestId(/^config-group-/)
    .flatMap((group) => within(group).queryAllByText(text));
}

export const GATEWAY = "cp-host";
export const AGENT_RUNNER = "runner-1";

export const MACHINES: AgentMachineRow[] = [
  { name: GATEWAY, description: "gateway", live: true, is_staging: false },
  { name: AGENT_RUNNER, description: "agent-runner", live: true, is_staging: false },
  { name: "offline-host", description: "down", live: false, is_staging: false },
];

export const STATUS = {
  cluster: { current_machine: GATEWAY },
} as unknown as SystemStatus;

export const OK_RESULT = (
  names: string[],
  restart: string[] = [],
): ConfigWriteResult => ({
  applied: true,
  results: Object.fromEntries(names.map((n) => [n, { ok: true, reason: null }])),
  restart_required: restart,
});

export const VIEW: ConfigView = {
  fields: [
    {
      name: "log_level",
      field_type: "string",
      current_value: "INFO",
      default_value: "INFO",
      description: "Logging verbosity",
      group: "LLM",
      capability: "agent-runner",
      scope: "cluster-default",
      restart_required: "all",
      writable: true,
      sensitive: false,
      env_var: "MEIVA_LOG_LEVEL",
      remote_writable: false,
      per_agent: true,
      can_enable: null,
      reason: null,
    },
    {
      name: "max_retries",
      field_type: "int",
      current_value: 3,
      default_value: 3,
      description: "Retry count",
      group: "LLM",
      capability: "agent-runner",
      scope: "cluster-default",
      restart_required: "",
      writable: true,
      sensitive: false,
      env_var: "MEIVA_MAX_RETRIES",
      remote_writable: false,
      per_agent: true,
      can_enable: null,
      reason: null,
    },
    {
      name: "enable_compact",
      field_type: "bool",
      current_value: true,
      default_value: false,
      description: "",
      group: "Agent",
      capability: "agent-runner",
      scope: "agent",
      restart_required: "",
      writable: true,
      sensitive: false,
      env_var: "MEIVA_ENABLE_COMPACT",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
    {
      name: "api_key",
      field_type: "string",
      current_value: "secret-xyz",
      default_value: "",
      description: "API key (sensitive)",
      group: "Agent",
      capability: "agent-runner",
      scope: "cluster-pinned",
      restart_required: "",
      writable: true,
      sensitive: true,
      env_var: "MEIVA_API_KEY",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
    {
      name: "build_sha",
      field_type: "string",
      current_value: "abc123",
      default_value: null,
      description: "Build hash",
      group: "General",
      capability: "common",
      scope: "host",
      restart_required: "",
      writable: false,
      sensitive: false,
      env_var: "MEIVA_BUILD_SHA",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
    {
      name: "host_label",
      field_type: "string",
      current_value: "rack-A",
      default_value: "",
      description: "Free-text label for the host",
      group: "General",
      capability: "common",
      scope: "host",
      restart_required: "gateway",
      writable: true,
      sensitive: false,
      env_var: "MEIVA_HOST_LABEL",
      remote_writable: true,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
    {
      name: "browser_enabled",
      field_type: "bool",
      current_value: false,
      default_value: false,
      description: "Headed browser",
      group: "Services",
      capability: "agent-runner",
      scope: "host",
      restart_required: "",
      writable: true,
      sensitive: false,
      env_var: "MEIVA_BROWSER_ENABLED",
      remote_writable: true,
      per_agent: false,
      can_enable: false,
      reason: "no display detected",
    },
    {
      name: "nudge_cadence",
      field_type: "enum",
      current_value: "once_per_compaction",
      default_value: "once_per_compaction",
      description: "How often the nudge fires",
      group: "Agent",
      capability: "agent-runner",
      scope: "cluster-default",
      restart_required: "agent",
      writable: true,
      sensitive: false,
      env_var: "MEIVA_NUDGE_CADENCE",
      remote_writable: false,
      per_agent: true,
      choices: ["once_per_compaction", "every_time"],
      can_enable: null,
      reason: null,
    },
    {
      name: "gateway_port",
      field_type: "int",
      current_value: 8000,
      default_value: 8000,
      description: "Gateway bind port",
      group: "Data plane",
      capability: "gateway",
      scope: "host",
      restart_required: "gateway",
      writable: false,
      sensitive: false,
      env_var: "MEIVA_GATEWAY_PORT",
      remote_writable: false,
      per_agent: false,
      can_enable: null,
      reason: null,
    },
  ],
  raw_overrides: { log_level: "INFO" },

  machine_capabilities: ["agent-runner"],
};

export const MODELS: ModelsResponse = {
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-5"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      context_window: 1_000_000,
      harnesses: ["meiva"],
      pricing: null,
      reasoning_effort_options: null,
    },
    "claude-opus-5": {
      provider: "claude",
      context_window: 1_000_000,
      harnesses: ["meiva"],
      pricing: null,
      reasoning_effort_options: null,
    },
  },
  default: "deepseek-v4-pro",
};

export const RESOLVED: ResolvedConfigView = {
  model: "deepseek-v4-pro",
  registered: true,
  fields: [
    {
      name: "auto_compact_fraction",
      env_var: "MEIVA_AUTO_COMPACT_FRACTION",
      description: "Force-compact ceiling",
      field_type: "float",
      choices: null,
      group: "Agent",
      effective_value: 0.55,
      source: "model-default",
      explicit_value: null,
      model_default: 0.55,
      shared_default: 0.8,
      per_agent: true,
      restart_required: "agent",
    },
    {
      name: "max_retries",
      env_var: "MEIVA_MAX_RETRIES",
      description: "Retry count",
      field_type: "int",
      choices: null,
      group: "LLM",
      effective_value: 9,
      source: "explicit",
      explicit_value: 9,
      model_default: 4,
      shared_default: 6,
      per_agent: false,
      restart_required: "agent",
    },
    {
      name: "llm_retry_max_attempts",
      env_var: "MEIVA_LLM_RETRY_MAX_ATTEMPTS",
      description: "Retry attempts",
      field_type: "int",
      choices: null,
      group: "LLM",
      effective_value: 6,
      source: "shared-default",
      explicit_value: null,
      model_default: null,
      shared_default: 6,
      per_agent: false,
      restart_required: "agent",
    },
  ],
};


export function setupConfigTest() {
  // meiva:note
  // restoreAllMocks + resetTransport: every test starts from unanswered endpoints, then
  // this harness answers the reads a config page always makes.
  vi.restoreAllMocks();
  resetTransport();
  localStorage.clear();
  vi.stubGlobal("requestIdleCallback", (cb: IdleRequestCallback) => {
    cb({ didTimeout: true, timeRemaining: () => 0 });
    return 0;
  });
  vi.stubGlobal("cancelIdleCallback", () => undefined);
  transport.getMachines.mockResolvedValue(MACHINES);
  transport.getSystemStatus.mockResolvedValue(STATUS);
  transport.getModels.mockResolvedValue(MODELS);
  transport.getResolvedConfig.mockResolvedValue(RESOLVED);
  transport.getDefaultModel.mockResolvedValue({
    model: "deepseek-v4-pro",
    source: "cluster",
  });
}
