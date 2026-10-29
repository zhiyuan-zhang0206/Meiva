import { modelLabel, reasoningEffortLabel } from "@/lib/contracts/models";
// meiva:note
// SpawnForm renders inside the sidebar's create popover, so every test mounts it
// directly. Agent processes only run on agent-runner machines: a form with no
// spawnable host says why instead of offering the table, one spawnable host makes
// the footer a single button, several list the hosts there. The form is one table
// (user 2026-10-20): harness, then the models that harness runs, then the efforts
// that model offers — each column narrows the next, and the stored spawn config
// marks its rows selected as soon as it renders.
// meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SpawnForm } from "./spawn-form";
import { USER_SETTING_DEFAULTS, type ModelsResponse, type SystemStatus } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import { renderWithApi as render } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getHarnessAvailability.mockResolvedValue([]);
  transport.getSettings.mockImplementation(() =>
      Promise.resolve({
        settings: Object.entries(storedSettings).map(([key, value]) => ({
          key,
          value,
          updated_at: "2026-05-19T00:00:00Z",
        })),
      }));
  transport.putSetting.mockImplementation((key: string, value: unknown) => {
      storedSettings = { ...storedSettings, [key]: value };
      setSettingCalls.push({ key, value });
      return Promise.resolve({ key, value, updated_at: "2026-05-19T00:00:00Z" });
    });
});


let storedSettings: Record<string, unknown> = {};
let setSettingCalls: { key: string; value: unknown }[] = [];

function resetMockSettings(initial?: Record<string, unknown>) {
  storedSettings = initial ? { ...initial } : {};
  setSettingCalls = [];
}

function setMockSetting(key: string, value: unknown) {
  storedSettings = { ...storedSettings, [key]: value };
}

function mockSetSettingCalls(): { key: string; value: unknown }[] {
  return setSettingCalls;
}


function wrap(layout?: "table" | "stack") {
  const onSpawn = vi.fn();
  const onDone = vi.fn();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  qc.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS, ...storedSettings });
  render(
    <QueryClientProvider client={qc}>
      <SpawnForm onSpawn={onSpawn} onDone={onDone} layout={layout} />
    </QueryClientProvider>,
  );
  return { onSpawn, onDone };
}

function statusWith(
  machines: {
    name: string;
    online: boolean;
    paused?: boolean | null;
    agentHostOnline?: boolean | null;
  }[],
  current = "cloud",
): SystemStatus {
  return {
    cluster: {
      current_machine: current,
      current_serve_gateway: true,
      current_serve_agent_runner: false,
      current_serve_observability_station: false,
      current_paused: false,
      machines: machines.map((m) => ({
        name: m.name,
        serve_gateway: m.name === current,
        serve_agent_runner: m.name !== current,
        serve_observability_station: false,
        identity_mismatch: false,
        is_staging: false,
        gateway_url: `http://${m.name}:8000`,
        up_since_at: "2026-05-19T00:00:00Z",
        online: m.online,
        paused: m.paused !== undefined ? m.paused : m.online ? false : null,
        agent_host_online: m.agentHostOnline !== undefined ? m.agentHostOnline : m.online ? true : null,
        shell_count: 0,
        agent_count: 0,
        session_count: 0,
        agent_groups: [],
        resource: null,
      })),
    },
    services: { items: [] },
  };
}

const singleMachineStatus = () =>
  statusWith([
    { name: "cloud", online: true },
    { name: "test-host", online: true },
  ]);

const modelsDefault = (): ModelsResponse => ({
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-4-8"] },
  models: {},
  default: "deepseek-v4-pro",
});

const modelsWithEffort = (): ModelsResponse => ({
  providers: { deepseek: ["deepseek-v4-pro"], claude: ["claude-opus-4-8"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      harnesses: ["meiva"],
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
      pricing: { input: 1.65, cache_read: 0.14, output: 6.6 },
    },
    "claude-opus-4-8": {
      provider: "claude",
      harnesses: ["meiva", "claude_code"],
      context_window: 200_000,
      reasoning_effort_options: null,
      reasoning_effort_default: null,
    },
  },
  default: "deepseek-v4-pro",
});

const modelsWithTwoLadders = (): ModelsResponse => ({
  providers: { deepseek: ["deepseek-v4-pro"], gpt: ["gpt-5.6"] },
  models: {
    "deepseek-v4-pro": {
      provider: "deepseek",
      harnesses: ["meiva"],
      context_window: 128_000,
      reasoning_effort_options: ["high", "max"],
      reasoning_effort_default: "max",
    },
    "gpt-5.6": {
      provider: "gpt",
      harnesses: ["meiva", "codex"],
      harness_settings: { codex: { reasoning_effort_options: ["low", "medium", "high"], reasoning_effort_default: "medium" } },
      context_window: 400_000,
      reasoning_effort_options: ["minimal", "low", "medium", "high"],
      reasoning_effort_default: "medium",
    },
  },
  default: "deepseek-v4-pro",
});

const harnessAllAvailable = () =>
  (["meiva", "claude_code", "codex", "antigravity", "kimi_code"] as const).map((harness) => ({
    harness,
    status: "available" as const,
    reason: "",
    launchable: true,
    billing: harness === "meiva" ? ("api" as const) : ("subscription" as const),
  }));

const MACHINE_COLUMN = "Machine";
const HARNESS_COLUMN = "Harness";
const MODEL_COLUMN = "Model";
const EFFORT_COLUMN = "Effort";

async function table(): Promise<HTMLElement> {
  return await screen.findByTestId("spawn-table");
}

function column(element: HTMLElement, name: string): HTMLElement {
  return within(element).getByRole("list", {
    name,
  });
}

function row(element: HTMLElement, name: string, option: string | RegExp): HTMLElement {
  const raw = typeof option === "string" ? option : option.source;
  const label = name === MODEL_COLUMN ? modelLabel(raw) : name === EFFORT_COLUMN ? reasoningEffortLabel(raw) : raw;
  return within(column(element, name)).getByRole("button", { name: typeof option === "string" ? label : new RegExp(label, option.flags) });
}

function selected(element: HTMLElement, name: string, option: string | RegExp): boolean {
  return row(element, name, option).getAttribute("aria-pressed") === "true";
}

function pick(element: HTMLElement, name: string, option: string | RegExp) {
  fireEvent.click(row(element, name, option));
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  resetMockSettings();
  transport.getModels.mockResolvedValue({ providers: {}, models: {}, default: "" });
  transport.getHarnessAvailability.mockResolvedValue(harnessAllAvailable());
});

describe("no spawnable agent host", () => {
  it("a reachable runner with a down host says so instead of showing the table", async () => {
    transport.getSystemStatus.mockResolvedValue(
      statusWith([{ name: "cloud", online: true }, { name: "test-host", online: true, agentHostOnline: false }]),
    );
    wrap();
    expect(await screen.findByText("Agent host unavailable")).toBeTruthy();
    expect(screen.queryByTestId("spawn-table")).toBeNull();
  });

  it("an absent host verdict reads as unknown", async () => {
    transport.getSystemStatus.mockResolvedValue(
      statusWith([{ name: "cloud", online: true }, { name: "test-host", online: true, agentHostOnline: null }]),
    );
    wrap();
    expect(await screen.findByText("Agent host status unknown")).toBeTruthy();
  });

  it("no agent-runner at all", async () => {
    transport.getSystemStatus.mockResolvedValue(statusWith([{ name: "cloud", online: true }]));
    wrap();
    expect(await screen.findByText("No agent runner with an available host")).toBeTruthy();
  });
});

describe("spawn table", () => {
  it("one spawnable runner → the footer spawns there with the resolved config", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    const { onSpawn, onDone } = wrap();
    const form = await table();
    expect(selected(form, MODEL_COLUMN, /deepseek-v4-pro/)).toBe(true);
    expect(selected(form, EFFORT_COLUMN, "max")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "deepseek-v4-pro",
      reasoning_effort: "max",
    });
    expect(onDone).toHaveBeenCalledOnce();
  });

  it("one spawnable host is just Spawn: the machine name is not a label", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsDefault());
    wrap();
    await table();
    // meiva:note
    // The footer names no machine when there is only one: the host name is a
    // deployment detail, and "Spawn on preview" read as if the product knew it
    // was a preview build (user 2026-10-20).
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    expect(screen.getByRole("button", { name: "Spawn" })).toBeTruthy();
    expect(screen.queryByText(/Spawn on/)).toBeNull();
  });

  it("opens with the stored spawn config marked selected", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    setMockSetting("behavior.spawn_harness", "claude_code");
    setMockSetting("behavior.spawn_model", "claude-opus-4-8");
    wrap();
    const form = await table();
    expect(selected(form, HARNESS_COLUMN, "Claude Code")).toBe(true);
    expect(selected(form, MODEL_COLUMN, /claude-opus-4-8/)).toBe(true);
    // meiva:note
    // A model without an effort ladder leaves the column empty.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    expect(within(column(form, EFFORT_COLUMN)).queryByRole("button")).toBeNull();
  });

  it.each(["claude_code", "codex"] as const)("names %s execution model and effort choices", async (harness) => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithTwoLadders());
    setMockSetting("behavior.spawn_harness", harness);
    wrap();
    const form = await table();
    expect(within(form).getByRole("list", { name: "Model" })).toBeTruthy();
    expect(within(form).getByRole("list", { name: "Effort" })).toBeTruthy();
    expect(within(form).queryByText("Meiva history model")).toBeNull();
    expect(within(form).queryByText("Summary effort")).toBeNull();
    pick(form, HARNESS_COLUMN, "Meiva");
    expect(within(form).getByRole("list", { name: "Model" })).toBeTruthy();
  });

  it("uses the harness effort ladder and sends the execution selection", async () => {
    const catalog = modelsWithTwoLadders();
    catalog.models["gpt-5.6"].harness_settings = {
      codex: { reasoning_effort_options: ["low", "ultra"], reasoning_effort_default: "low" },
    };
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(catalog);
    setMockSetting("behavior.spawn_reasoning_effort", "minimal");
    const { onSpawn } = wrap();
    const form = await table();
    pick(form, HARNESS_COLUMN, "Codex");
    expect(selected(form, EFFORT_COLUMN, "low")).toBe(true);
    expect(within(column(form, EFFORT_COLUMN)).queryByRole("button", { name: "Minimal" })).toBeNull();
    expect(within(column(form, EFFORT_COLUMN)).queryByRole("button", { name: "Medium" })).toBeNull();
    pick(form, EFFORT_COLUMN, "ultra");
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host", harness: "codex", model: "gpt-5.6", reasoning_effort: "ultra",
    });
  });

  it("a pick that is never spawned does not become the stored config", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsDefault());
    setMockSetting("behavior.spawn_model", "deepseek-v4-pro");
    wrap();
    const form = await table();
    pick(form, MODEL_COLUMN, /claude-opus-4-8/);
    expect(selected(form, MODEL_COLUMN, /claude-opus-4-8/)).toBe(true);
    // meiva:note
    // Closing without spawning keeps the previous spawn's config: the next open
    // must answer "what did the last agent run on", not "what did I click last"
    // (user 2026-10-20). A click that wrote through would surface here.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    cleanup();
    wrap();
    const reopened = await table();
    expect(selected(reopened, MODEL_COLUMN, /deepseek-v4-pro/)).toBe(true);
  });

  it("a spawn stores the config it used, cascade included", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    wrap();
    const form = await table();
    pick(form, EFFORT_COLUMN, "high");
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    // meiva:note
    // No stored model or effort means the cascade filled them in; the spawn
    // writes what it actually sent, so the next open marks the same rows.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    await waitFor(() =>
      expect(mockSetSettingCalls()).toEqual([
        { key: "behavior.spawn_model", value: "deepseek-v4-pro" },
        { key: "behavior.spawn_reasoning_effort", value: "high" },
      ]),
    );
  });

  it("changing the harness keeps only the models that harness runs", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    const { onSpawn } = wrap();
    const form = await table();
    expect(row(form, MODEL_COLUMN, /deepseek-v4-pro/)).toBeTruthy();
    pick(form, HARNESS_COLUMN, "Claude Code");
    expect(within(column(form, MODEL_COLUMN)).queryByText(modelLabel("deepseek-v4-pro"))).toBeNull();
    expect(selected(form, MODEL_COLUMN, /claude-opus-4-8/)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith({
      machine: "test-host",
      model: "claude-opus-4-8",
      reasoning_effort: undefined,
      harness: "claude_code",
    });
  });

  it("follows the registry: a model assigned to another harness shows up there", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    // meiva:note
    // The registry - not this frontend - decides which harness runs which model: a
    // model whose published harnesses name an external CLI appears in that CLI's
    // column with no frontend change, and one that does not is absent from it. The
    // DeepSeek model is deliberately not the CLI's own vendor.
    // meiva:link ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md
    transport.getModels.mockResolvedValue({
      providers: { deepseek: ["deepseek-v4-pro"], gpt: ["gpt-5.6"] },
      models: {
        "deepseek-v4-pro": {
          provider: "deepseek",
          harnesses: ["meiva", "codex"],
          context_window: 128_000,
        },
        "gpt-5.6": { provider: "gpt", harnesses: ["meiva"], context_window: 400_000 },
      },
      default: "deepseek-v4-pro",
    });
    wrap();
    const form = await table();
    expect(within(column(form, MODEL_COLUMN)).queryByText(modelLabel("gpt-5.6"))).toBeTruthy();
    pick(form, HARNESS_COLUMN, "Codex");
    expect(within(column(form, MODEL_COLUMN)).queryByText(modelLabel("deepseek-v4-pro"))).toBeTruthy();
    expect(within(column(form, MODEL_COLUMN)).queryByText(modelLabel("gpt-5.6"))).toBeNull();
    expect(selected(form, MODEL_COLUMN, /deepseek-v4-pro/)).toBe(true);
  });

  it("a harness the machine cannot start is disabled in its column", async () => {
    transport.getSystemStatus.mockResolvedValue(singleMachineStatus());
    transport.getModels.mockResolvedValue(modelsWithEffort());
    transport.getHarnessAvailability.mockResolvedValue([
      { harness: "meiva", status: "available", reason: "", launchable: true, billing: "api" },
      { harness: "claude_code", status: "unavailable", reason: "not signed in", launchable: false, billing: "subscription" },
    ]);
    wrap();
    const form = await table();
    await waitFor(() => {
      expect(row(form, HARNESS_COLUMN, /Claude Code/).hasAttribute("disabled")).toBe(true);
    });
  });

  it("switching to a machine that cannot start the chosen harness falls back to one it can", async () => {
    transport.getSystemStatus.mockResolvedValue(
      statusWith([
        { name: "cloud", online: true },
        { name: "a-host", online: true },
        { name: "c-host", online: true },
      ]),
    );
    transport.getModels.mockResolvedValue(modelsDefault());
    transport.getHarnessAvailability.mockImplementation((machine: string) =>
      Promise.resolve(
        machine === "c-host"
          ? [{ harness: "meiva", status: "available", reason: "", launchable: true, billing: "api" },
             { harness: "claude_code", status: "unavailable", reason: "missing", launchable: false, billing: "subscription" }]
          : harnessAllAvailable(),
      ),
    );
    const { onSpawn } = wrap();
    const form = await table();
    pick(form, HARNESS_COLUMN, "Claude Code");
    pick(form, MACHINE_COLUMN, "c-host");
    await waitFor(() => expect(selected(form, HARNESS_COLUMN, "Meiva")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Spawn" }));
    expect(onSpawn).toHaveBeenCalledWith(expect.objectContaining({ machine: "c-host" }));
    const opts = onSpawn.mock.calls[0][0] as { harness?: string };
    expect(opts.harness).toBeUndefined();
  });
});
